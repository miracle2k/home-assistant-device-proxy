"""On/off controls that follow the confirmed physical source mapping."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ProxyConfigEntry
from .entity import ProxyEntity


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
            for channel in target["channels"]:
                key = (target_id, channel["id"])
                if channel["domain"] == "switch" and key not in added:
                    added.add(key)
                    entities.append(ProxySwitch(coordinator, target_id, channel))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(add_new))
    add_new()


class ProxySwitch(ProxyEntity, SwitchEntity):
    def __init__(self, coordinator, target_id, channel) -> None:
        super().__init__(coordinator, target_id, channel)
        self._attr_device_class = channel["device_class"]

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.current_value(self.target_id, self.channel)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_switch(
            self.target_id, self.channel["id"], "turn_on", self._context
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_control_switch(
            self.target_id, self.channel["id"], "turn_off", self._context
        )
