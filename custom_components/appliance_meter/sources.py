"""Discover typed readings and follow their registry identities."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from homeassistant.components.sensor.const import (
    DEVICE_CLASS_STATE_CLASSES,
    NON_NUMERIC_DEVICE_CLASSES,
    UNIT_CONVERTERS,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import DOMAIN, INCREASE, MIRROR, NET


def number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def device_name(hass: HomeAssistant, device_id: str | None) -> str:
    device = dr.async_get(hass).async_get(device_id) if device_id else None
    return (
        (device.name_by_user or device.name or "Unnamed device")
        if device
        else "Unavailable device"
    )


def describe(hass: HomeAssistant, registry_id: str) -> dict[str, Any] | None:
    """Registry metadata works before startup and while the source is offline."""
    entry = er.async_get(hass).async_get(registry_id)
    if entry is None or entry.disabled or entry.platform == DOMAIN:
        return None
    if entry.domain not in ("sensor", "binary_sensor", "switch"):
        return None
    attrs = dict(entry.capabilities or {})
    attrs.update(
        {
            "device_class": entry.device_class or entry.original_device_class,
            "unit_of_measurement": entry.unit_of_measurement,
            "icon": entry.icon or entry.original_icon,
        }
    )
    state = hass.states.get(entry.entity_id)
    if state:
        attrs.update(state.attributes)
    name = entry.name or entry.original_name or attrs.get("friendly_name")
    name = name or entry.entity_id.split(".", 1)[1].replace("_", " ").capitalize()
    device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
    if device:
        if entry.domain == "switch" and name.casefold() in {
            value.casefold() for value in (device.name_by_user, device.name) if value
        }:
            name = "On/off"
        for prefix in (device.name_by_user, device.name):
            if prefix and name.casefold().startswith(prefix.casefold() + " "):
                name = name[len(prefix) :].strip(" :·-") or name
    return {
        "registry_id": entry.id,
        "entity_id": entry.entity_id,
        "device_id": entry.device_id,
        "name": name,
        "domain": entry.domain,
        "device_class": attrs.get("device_class"),
        "unit": attrs.get("unit_of_measurement"),
        "input_state_class": attrs.get("state_class"),
        "options": list(attrs.get("options") or []),
        "icon": attrs.get("icon"),
        "entity_category": entry.entity_category,
    }


def discover(hass: HomeAssistant, device_id: str) -> dict[str, dict[str, Any]]:
    """Enabled readings and optional on/off controls on a source device."""
    return {
        info["registry_id"]: info
        for entry in er.async_entries_for_device(er.async_get(hass), device_id)
        if (info := describe(hass, entry.id)) is not None
    }


def allowed_modes(info: dict[str, Any]) -> list[str]:
    if info["domain"] != "sensor" or info["device_class"] in NON_NUMERIC_DEVICE_CLASSES:
        return [MIRROR]
    classes = DEVICE_CLASS_STATE_CLASSES.get(info["device_class"])
    result = [MIRROR]
    if classes is None or "total_increasing" in classes:
        result.append(INCREASE)
    if classes is None or "total" in classes:
        result.append(NET)
    return result


def channel_from_source(
    info: dict[str, Any], *, channel_id: str | None = None
) -> dict[str, Any]:
    """Copy the source contract; channel identity stays stable after assignment."""
    mode = {"total_increasing": INCREASE, "total": NET}.get(
        info["input_state_class"], MIRROR
    )
    return set_mode(
        {
            "id": channel_id or uuid4().hex,
            "name": info["name"][:80],
            **{
                key: info[key]
                for key in (
                    "domain",
                    "device_class",
                    "unit",
                    "input_state_class",
                    "options",
                    "icon",
                    "entity_category",
                )
            },
        },
        mode,
    )


def set_mode(channel: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in allowed_modes(channel):
        raise ValueError("invalid_mode")
    return {
        **channel,
        "mode": mode,
        "state_class": {INCREASE: "total_increasing", NET: "total"}.get(
            mode, channel["input_state_class"]
        ),
    }


def compatible(channel: dict[str, Any], source: dict[str, Any]) -> bool:
    """Keep existing histories on a fixed type, statistical meaning, and unit."""
    if (
        channel["domain"] != source["domain"]
        or channel["device_class"] != source["device_class"]
    ):
        return False
    if channel["input_state_class"] != source["input_state_class"]:
        return False
    if channel["device_class"] == "enum" and not set(source["options"]) <= set(
        channel["options"]
    ):
        return False
    if channel["unit"] == source["unit"]:
        return True
    converter = UNIT_CONVERTERS.get(channel["device_class"])
    return bool(
        converter
        and channel["unit"] in converter.VALID_UNITS
        and source["unit"] in converter.VALID_UNITS
    )


def source_state(hass: HomeAssistant, source_id: str | None) -> State | None:
    entry = er.async_get(hass).async_get(source_id) if source_id else None
    return hass.states.get(entry.entity_id) if entry and not entry.disabled else None


def read_value(
    hass: HomeAssistant, source_id: str | None, channel: dict[str, Any]
) -> Any:
    """Convert a fresh source value into the virtual channel's fixed native unit."""
    state = source_state(hass, source_id)
    if (
        state is None
        or state.state in ("unknown", "unavailable")
        or state.attributes.get("restored")
    ):
        return None
    info = describe(hass, source_id)
    if info is None or not compatible(channel, info):
        return None
    if channel["domain"] in ("binary_sensor", "switch"):
        return state.state == "on" if state.state in ("on", "off") else None
    kind = channel["device_class"]
    if kind == "date":
        try:
            return date.fromisoformat(state.state)
        except ValueError:
            return None
    if kind == "timestamp":
        value = dt_util.parse_datetime(state.state)
        return value if value is not None and value.tzinfo is not None else None
    if kind == "enum":
        return state.state if state.state in channel["options"] else None
    numeric = (
        channel["mode"] != MIRROR
        or channel["unit"] is not None
        or (channel["state_class"] is not None or kind is not None)
    )
    if not numeric:
        return state.state
    value = number(state.state)
    if value is None:
        return None
    if info["unit"] != channel["unit"]:
        converter = UNIT_CONVERTERS.get(kind)
        try:
            if converter is TemperatureConverter:
                value = Decimal(
                    str(converter.convert(float(value), info["unit"], channel["unit"]))
                )
            else:
                value /= Decimal(
                    str(converter.get_unit_ratio(info["unit"], channel["unit"]))
                )
        except HomeAssistantError, ArithmeticError:
            return None
    return value


def reset_marker(state: State | None) -> str | None:
    value = state.attributes.get("last_reset") if state else None
    if value and (parsed := dt_util.parse_datetime(str(value))):
        return dt_util.as_utc(parsed).isoformat()
    return None
