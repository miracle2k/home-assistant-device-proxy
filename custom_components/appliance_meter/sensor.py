"""Typed sensor proxies, independently allocated counters, and source status."""

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ProxyConfigEntry
from .const import MIRROR
from .coordinator import ProxyCoordinator
from .entity import ProxyEntity
from .sources import device_name, source_state


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    added = set()

    @callback
    def add_new() -> None:
        entities = []
        for target_id, target in coordinator.devices.items():
            if (target_id, "__status__") not in added:
                added.add((target_id, "__status__"))
                entities.append(SourceStatus(coordinator, target_id))
            for channel in target["channels"]:
                key = (target_id, channel["id"])
                if channel["domain"] == "sensor" and key not in added:
                    added.add(key)
                    entities.append(ProxySensor(coordinator, target_id, channel))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(add_new))
    add_new()


class ProxySensor(ProxyEntity, SensorEntity):
    def __init__(
        self, coordinator: ProxyCoordinator, target_id: str, channel: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, target_id, channel)
        self._attr_device_class = channel["device_class"]
        self._attr_native_unit_of_measurement = channel["unit"]
        self._attr_state_class = channel["state_class"]
        self._attr_options = (
            channel["options"] if channel["device_class"] == "enum" else None
        )
        if channel["mode"] != MIRROR:
            self._attr_suggested_display_precision = 3 if channel["unit"] else 0

    @property
    def native_value(self) -> Any:
        return self.coordinator.current_value(self.target_id, self.channel)

    @property
    def last_reset(self):
        if self.channel["mode"] != MIRROR or self.channel["state_class"] != "total":
            return None
        state = source_state(
            self.hass, self.coordinator.source_id(self.target_id, self.channel["id"])
        )
        value = state.attributes.get("last_reset") if state else None
        return dt_util.parse_datetime(str(value)) if value else None


class SourceStatus(ProxyEntity, SensorEntity):
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, coordinator: ProxyCoordinator, target_id: str) -> None:
        super().__init__(
            coordinator, target_id, {"id": "source_status", "name": "Source device"}
        )
        self._attr_icon = "mdi:swap-horizontal"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        binding = self.coordinator.binding(self.target_id)
        return device_name(self.hass, binding["device_id"]) if binding else "Unassigned"

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {
            "assignment_ui": "Settings → Devices & services → Device Proxy → Configure"
        }
