"""Shared lifecycle and identity for virtual readings."""

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, NAME
from .coordinator import ProxyCoordinator
from .sources import describe, device_name, source_state

SYSTEM_ATTRIBUTES = {
    "friendly_name",
    "icon",
    "device_class",
    "state_class",
    "unit_of_measurement",
    "options",
    "restored",
    "last_reset",
    "supported_features",
}


class ProxyEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ProxyCoordinator, target_id: str, channel: dict[str, Any]
    ) -> None:
        self.coordinator = coordinator
        self.target_id = target_id
        self.channel = channel
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{target_id}_{channel['id']}"
        )
        self._attr_name = channel["name"]
        self._attr_icon = channel.get("icon")
        self._attr_entity_category = channel.get("entity_category")

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={
                (DOMAIN, f"{self.coordinator.entry.entry_id}_{self.target_id}")
            },
            name=self.coordinator.devices[self.target_id]["name"],
            manufacturer=NAME,
            model="Virtual device",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self.coordinator.current_value(self.target_id, self.channel) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        binding = self.coordinator.binding(self.target_id)
        source_id = self.coordinator.source_id(self.target_id, self.channel["id"])
        state = source_state(self.hass, source_id)
        attrs = {}
        if self.available and self.channel["mode"] == "mirror" and state:
            attrs = {
                key: value
                for key, value in state.attributes.items()
                if key not in SYSTEM_ATTRIBUTES
            }
        info = describe(self.hass, source_id) if source_id else None
        return {
            **attrs,
            "source_device": device_name(self.hass, binding["device_id"])
            if binding
            else None,
            "source_entity": info["entity_id"] if info else None,
            "source_last_updated": state.last_updated.isoformat() if state else None,
            "proxy_mode": self.channel["mode"],
        }
