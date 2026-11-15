"""Persistent virtual devices and reviewed assignment plans."""

from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any

from .const import INCREASE, MIRROR, MODES
from .ledger import CounterLedger
from .sources import number, set_mode


@dataclass
class AssignmentPlan:
    target: dict[str, Any]
    source_device: str | None
    mapping: dict[str, str | None]
    revision: int
    operation: str = "assign"


def slot(target_id: str, channel_id: str) -> str:
    return f"{target_id}:{channel_id}"


def empty_state() -> dict[str, Any]:
    return {
        "devices": {},
        "assignments": {},
        "saved_mappings": {},
        "counters": {},
        "revision": 0,
    }


def check_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 80 or "\n" in name or "\r" in name:
        raise ValueError("invalid_name")
    return name


def initial_state(plan: AssignmentPlan, timestamp: float) -> dict[str, Any]:
    state = empty_state()
    target_id = plan.target["id"]
    state["devices"][target_id] = deepcopy(plan.target)
    if plan.source_device:
        binding = {
            "device_id": plan.source_device,
            "mapping": dict(plan.mapping),
            "since": timestamp,
        }
        state["assignments"][target_id] = binding
        state["saved_mappings"][target_id] = {plan.source_device: dict(plan.mapping)}
    state["revision"] = 1
    validate_state(state)
    return state


def validate_state(state: dict[str, Any]) -> None:
    """A malformed checkpoint must fail setup instead of discarding history."""
    if set(state) != {
        "devices",
        "assignments",
        "saved_mappings",
        "counters",
        "revision",
    }:
        raise ValueError("Invalid Device Proxy checkpoint")
    if not isinstance(state["revision"], int) or state["revision"] < 0:
        raise ValueError("Invalid revision")
    names = set()
    slots = {}
    for target_id, target in state["devices"].items():
        if target["id"] != target_id or not target_id or ":" in target_id:
            raise ValueError("Invalid virtual device ID")
        name = check_name(target["name"])
        if name.casefold() in names:
            raise ValueError("Duplicate virtual device name")
        names.add(name.casefold())
        channel_ids = set()
        for channel in target["channels"]:
            channel_id = channel["id"]
            if not channel_id or ":" in channel_id or channel_id in channel_ids:
                raise ValueError("Invalid channel ID")
            channel_ids.add(channel_id)
            check_name(channel["name"])
            if (
                channel["domain"] not in ("sensor", "binary_sensor", "switch")
                or channel["mode"] not in MODES
            ):
                raise ValueError("Invalid channel type")
            if channel != set_mode(channel, channel["mode"]):
                raise ValueError("Invalid channel statistics")
            slots[slot(target_id, channel_id)] = channel["mode"]
    used_devices = set()
    for target_id, binding in state["assignments"].items():
        target = state["devices"][target_id]
        device_id = binding["device_id"]
        if not device_id or device_id in used_devices:
            raise ValueError("A physical source can feed only one virtual device")
        used_devices.add(device_id)
        keys = {channel["id"] for channel in target["channels"]}
        if (
            set(binding["mapping"]) != keys
            or not isinstance(binding["since"], (int, float))
            or not isfinite(binding["since"])
        ):
            raise ValueError("Invalid assignment")
        values = [value for value in binding["mapping"].values() if value]
        if len(values) != len(set(values)):
            raise ValueError("A source reading cannot feed two channels")
    ledger = CounterLedger(state["counters"])
    for key, counter in ledger.counters.items():
        if slots.get(key) != counter.mode:
            raise ValueError("Invalid counter channel")
    for target_id, devices in state["saved_mappings"].items():
        keys = {channel["id"] for channel in state["devices"][target_id]["channels"]}
        for mapping in devices.values():
            if not set(mapping) <= keys:
                raise ValueError("Unknown remembered channel")


def migrate_legacy(
    config: dict[str, Any], checkpoint: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep every v1 device, sensor unique ID, total, and active baseline."""
    legacy_metrics = {
        "energy": ("Energy", "energy", "kWh"),
        "power": ("Power", "power", "W"),
        "current": ("Current", "current", "A"),
        "voltage": ("Voltage", "voltage", "V"),
        "apparent_power": ("Apparent power", "apparent_power", "VA"),
        "reactive_power": ("Reactive power", "reactive_power", "var"),
        "frequency": ("Frequency", "frequency", "Hz"),
        "power_factor": ("Power factor", "power_factor", "%"),
    }
    state = empty_state()
    checkpoint = checkpoint or {"totals": {}, "meters": {}}
    ledger = CounterLedger()
    for appliance in config["appliances"]:
        target_id = appliance["id"]
        channels = []
        for metric in config["metrics"]:
            name, kind, unit = legacy_metrics[metric]
            channels.append(
                set_mode(
                    {
                        "id": metric,
                        "name": name,
                        "domain": "sensor",
                        "device_class": kind,
                        "unit": unit,
                        "input_state_class": "total_increasing"
                        if metric == "energy"
                        else "measurement",
                        "options": [],
                        "icon": None,
                        "entity_category": None,
                    },
                    INCREASE if metric == "energy" else MIRROR,
                )
            )
        state["devices"][target_id] = {
            "id": target_id,
            "name": appliance["name"],
            "channels": channels,
        }
        if "energy" in config["metrics"]:
            counter = ledger.ensure(slot(target_id, "energy"), INCREASE)

            total = number(checkpoint["totals"].get(target_id, 0))
            if total is None or total < 0:
                raise ValueError("Invalid legacy total")
            counter.total = total
        state["saved_mappings"][target_id] = {}
        for meter in config["meters"]:
            mapping = {
                channel["id"]: meter["sources"]
                .get(channel["id"], {})
                .get("registry_id")
                for channel in channels
            }
            state["saved_mappings"][target_id][meter["device_id"]] = mapping
            saved = checkpoint["meters"].get(meter["id"], {})
            if saved.get("appliance") == target_id:
                state["assignments"][target_id] = {
                    "device_id": meter["device_id"],
                    "mapping": dict(mapping),
                    "since": saved["assigned_at"],
                }
                if "energy" in config["metrics"]:
                    baseline = number(saved["baseline"])
                    if saved["baseline"] is not None and baseline is None:
                        raise ValueError("Invalid legacy baseline")
                    counter = ledger.ensure(slot(target_id, "energy"), INCREASE)
                    counter.source = mapping["energy"]
                    counter.baseline = baseline
                    counter.last_seen = saved["last_seen"]
                    counter.pending_reset = number(saved.get("pending_reset"))
    state["counters"] = ledger.as_dict()
    state["revision"] = 1
    validate_state(state)
    return state
