"""Read-only binary sensor proxies."""

from homeassistant.components.binary_sensor import BinarySensorEntity
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
                if channel["domain"] == "binary_sensor" and key not in added:
                    added.add(key)
                    entities.append(ProxyBinarySensor(coordinator, target_id, channel))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(add_new))
    add_new()


class ProxyBinarySensor(ProxyEntity, BinarySensorEntity):
    def __init__(self, coordinator, target_id, channel):
        super().__init__(coordinator, target_id, channel)
        self._attr_device_class = channel["device_class"]

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.current_value(self.target_id, self.channel)
