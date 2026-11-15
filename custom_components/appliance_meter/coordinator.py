"""Apply reviewed assignments and allocate every counter in one checkpoint."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MIRROR, SAVE_INTERVAL
from .ledger import CounterLedger
from .model import AssignmentPlan, check_name, slot, validate_state
from .sources import compatible, describe, read_value, reset_marker, source_state
from .storage import ProxyStore


class DraftConflict(HomeAssistantError):
    """Another review was applied after this wizard started."""


def validate_assignment(
    hass: HomeAssistant, data: dict[str, Any], plan: AssignmentPlan
) -> None:
    if plan.revision != data["revision"]:
        raise DraftConflict("The configuration changed. Close and restart the review.")
    target_id = plan.target["id"]
    name = check_name(plan.target["name"])
    if any(
        key != target_id and value["name"].casefold() == name.casefold()
        for key, value in data["devices"].items()
    ):
        raise ValueError("duplicate_name")
    old = data["devices"].get(target_id)
    channels = {channel["id"]: channel for channel in plan.target["channels"]}
    if old and any(
        channels.get(channel["id"]) != channel for channel in old["channels"]
    ):
        raise ValueError("Existing reading types and units cannot change")
    if set(plan.mapping) != set(channels):
        raise ValueError("Invalid mapping")
    mapped = [value for value in plan.mapping.values() if value]
    if len(mapped) != len(set(mapped)):
        raise ValueError("duplicate_source")
    if plan.operation not in ("assign", "unassign", "rename"):
        raise ValueError("Invalid operation")
    if plan.operation == "rename":
        binding = data["assignments"].get(target_id)
        source = binding["device_id"] if binding else None
        mapping = binding["mapping"] if binding else dict.fromkeys(channels)
        if (
            old is None
            or source != plan.source_device
            or mapping != plan.mapping
            or old["channels"] != plan.target["channels"]
        ):
            raise ValueError("Invalid rename")
        return
    if plan.operation == "unassign" and plan.source_device is not None:
        raise ValueError("Invalid unassignment")
    if not plan.source_device:
        if mapped:
            raise ValueError("Invalid unmapped assignment")
    elif dr.async_get(hass).async_get(plan.source_device) is None:
        raise ValueError("source_changed")
    for channel_id, source_id in plan.mapping.items():
        if source_id is None:
            continue
        info = describe(hass, source_id)
        if (
            info is None
            or info["device_id"] != plan.source_device
            or not compatible(channels[channel_id], info)
        ):
            raise ValueError("source_changed")


class ProxyCoordinator:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = ProxyStore(hass, entry)
        self.data: dict[str, Any] = {}
        self.ledger = CounterLedger()
        self._lock = asyncio.Lock()
        self._listeners: set[Callable[[], None]] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._registry_unsub: Callable[[], None] | None = None
        self._tracked: set[str] = set()
        self._save_scheduled = False
        self._stopping = False

    @property
    def devices(self) -> dict[str, Any]:
        return self.data["devices"]

    @property
    def revision(self) -> int:
        return self.data["revision"]

    async def async_start(self) -> None:
        data = await self.store.async_load()
        if data is None:
            if self.entry.data.get("initialized") or "initial" not in self.entry.data:
                raise ValueError(
                    "The Device Proxy checkpoint is missing; restore a backup"
                )
            data = deepcopy(self.entry.data["initial"])
        validate_state(data)
        self.data = data
        self.ledger = CounterLedger(data["counters"])
        self._sample_all()
        await self.store.async_save(self._snapshot())
        if not self.entry.data.get("initialized"):
            self.hass.config_entries.async_update_entry(
                self.entry, data={"initialized": True}
            )
        self._subscribe()
        self._registry_unsub = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_changed
        )

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def binding(self, target_id: str) -> dict[str, Any] | None:
        return self.data["assignments"].get(target_id)

    def source_id(self, target_id: str, channel_id: str) -> str | None:
        binding = self.binding(target_id)
        return binding["mapping"].get(channel_id) if binding else None

    def current_value(self, target_id: str, channel: dict[str, Any]) -> Any:
        if channel["mode"] != MIRROR:
            return self.ledger.total(slot(target_id, channel["id"]))
        binding = self.binding(target_id)
        source_id = self.source_id(target_id, channel["id"])
        if binding is None:
            return None
        return read_value(self.hass, source_id, channel)

    async def async_control_switch(
        self,
        target_id: str,
        channel_id: str,
        service: str,
        context: Context | None,
    ) -> None:
        """Hold the reviewed mapping until the on/off command has completed."""
        async with self._lock:
            target = self.devices.get(target_id)
            channel = (
                next(
                    (item for item in target["channels"] if item["id"] == channel_id),
                    None,
                )
                if target
                else None
            )
            binding = self.binding(target_id)
            if (
                self._stopping
                or channel is None
                or channel["domain"] != "switch"
                or service not in ("turn_on", "turn_off")
                or binding is None
            ):
                raise HomeAssistantError("This virtual switch has no available source")
            source_id = self.source_id(target_id, channel_id)
            info = describe(self.hass, source_id) if source_id else None
            if (
                info is None
                or info["device_id"] != binding["device_id"]
                or read_value(self.hass, source_id, channel) is None
            ):
                raise HomeAssistantError("The mapped source switch is unavailable")
            await self.hass.services.async_call(
                "switch",
                service,
                {"entity_id": info["entity_id"]},
                blocking=True,
                context=context,
            )

    def _sample_all(self) -> bool:
        changed = False
        for target_id, target in self.devices.items():
            for channel in target["channels"]:
                if channel["mode"] == MIRROR:
                    continue
                key = slot(target_id, channel["id"])
                counter = self.ledger.ensure(key, channel["mode"])
                source_id = self.source_id(target_id, channel["id"])
                if not source_id:
                    continue
                state = source_state(self.hass, source_id)
                value = read_value(self.hass, source_id, channel)
                timestamp = state.last_reported.timestamp() if state else 0
                if counter.source != source_id:
                    self.ledger.rebind(
                        key,
                        channel["mode"],
                        source_id,
                        value,
                        timestamp,
                        reset_marker(state),
                    )
                    changed = True
                else:
                    changed = (
                        self.ledger.observe(key, value, timestamp, reset_marker(state))
                        or changed
                    )
        return changed

    @callback
    def _subscribe(self) -> None:
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._tracked = {
            state.entity_id
            for binding in self.data["assignments"].values()
            for source_id in binding["mapping"].values()
            if (state := source_state(self.hass, source_id)) is not None
        }
        # Track registry identities even before their entities have a state.
        registry = er.async_get(self.hass)
        self._tracked.update(
            entry.entity_id
            for binding in self.data["assignments"].values()
            for source_id in binding["mapping"].values()
            if source_id
            and (entry := registry.async_get(source_id)) is not None
            and not entry.disabled
        )
        self._unsubs = [
            async_track_state_change_event(
                self.hass, self._tracked, self._async_source_event
            ),
            async_track_state_report_event(
                self.hass, self._tracked, self._async_source_event
            ),
        ]

    async def _async_registry_changed(self, event: Event) -> None:
        if self._stopping:
            return
        registry = er.async_get(self.hass)
        resolved = {
            entry.entity_id
            for binding in self.data["assignments"].values()
            for source_id in binding["mapping"].values()
            if source_id
            and (entry := registry.async_get(source_id)) is not None
            and not entry.disabled
        }
        if resolved != self._tracked:
            self._subscribe()
            await self._async_source_event()

    async def _async_source_event(self, event: Event | None = None) -> None:
        async with self._lock:
            if self._stopping:
                return
            if self._sample_all():
                self._schedule_save()
            self._notify()

    def _snapshot(self) -> dict[str, Any]:
        return {**deepcopy(self.data), "counters": self.ledger.as_dict()}

    @callback
    def _checkpoint(self) -> dict[str, Any]:
        self._save_scheduled = False
        return self._snapshot()

    @callback
    def _schedule_save(self) -> None:
        if not self._save_scheduled:
            self._save_scheduled = True
            self.store.async_delay_save(self._checkpoint, SAVE_INTERVAL)

    def validate_plan(self, plan: AssignmentPlan) -> None:
        validate_assignment(self.hass, self.data, plan)

    async def async_commit(self, plan: AssignmentPlan) -> None:
        """The wizard calls this only after its explicit final confirmation."""
        async with self._lock:
            if self._stopping:
                raise HomeAssistantError("Device Proxy is unloading")
            self.validate_plan(plan)
            self._sample_all()
            before = self._snapshot()
            uncertain = {
                counter.source
                for counter in self.ledger.counters.values()
                if counter.pending_reset is not None
            }
            target_id = plan.target["id"]
            previous = self.binding(target_id)
            same = bool(
                previous
                and previous["device_id"] == plan.source_device
                and previous["mapping"] == plan.mapping
            )
            now = dt_util.utcnow().timestamp()
            try:
                self.data["devices"][target_id] = deepcopy(plan.target)
                if plan.source_device:
                    self.data["assignments"] = {
                        key: binding
                        for key, binding in self.data["assignments"].items()
                        if key == target_id
                        or binding["device_id"] != plan.source_device
                    }
                    self.data["assignments"][target_id] = {
                        "device_id": plan.source_device,
                        "mapping": dict(plan.mapping),
                        "since": previous["since"] if same else now,
                    }
                    self.data["saved_mappings"].setdefault(target_id, {})[
                        plan.source_device
                    ] = dict(plan.mapping)
                else:
                    self.data["assignments"].pop(target_id, None)
                for channel in plan.target["channels"]:
                    if channel["mode"] == MIRROR:
                        continue
                    key = slot(target_id, channel["id"])
                    counter = self.ledger.ensure(key, channel["mode"])
                    source_id = (
                        plan.mapping[channel["id"]] if plan.source_device else None
                    )
                    unchanged = bool(
                        previous
                        and previous["device_id"] == plan.source_device
                        and previous["mapping"].get(channel["id"]) == source_id
                        and counter.source == source_id
                    )
                    if not unchanged:
                        state = source_state(self.hass, source_id)
                        value = (
                            None
                            if source_id in uncertain
                            else read_value(self.hass, source_id, channel)
                        )
                        self.ledger.rebind(
                            key,
                            channel["mode"],
                            source_id,
                            value,
                            state.last_reported.timestamp() if state else 0,
                            reset_marker(state),
                        )
                self.data["revision"] += 1
                snapshot = self._snapshot()
                validate_state(snapshot)
                await self.store.async_save(snapshot)
            except Exception as err:
                self.data = before
                self.ledger = CounterLedger(before["counters"])
                self._save_scheduled = False
                self._schedule_save()
                raise HomeAssistantError(
                    "Could not save the change. The previous assignment is kept."
                ) from err
            self._save_scheduled = False
            self._subscribe()
            # Update the default device name; preserve user-set entity names.
            registry = dr.async_get(self.hass)
            if device := registry.async_get_device(
                identifiers={(DOMAIN, f"{self.entry.entry_id}_{target_id}")}
            ):
                registry.async_update_device(device.id, name=plan.target["name"])
            self._notify()

    async def async_stop(self) -> None:
        async with self._lock:
            self._sample_all()
            self._stopping = True
            for unsubscribe in self._unsubs:
                unsubscribe()
            self._unsubs.clear()
            if self._registry_unsub:
                self._registry_unsub()
                self._registry_unsub = None
            await self.store.async_save(self._snapshot())
            self._save_scheduled = False
