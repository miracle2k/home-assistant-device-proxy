"""Device Proxy: virtual identities with reviewed source assignments."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er

from .coordinator import ProxyCoordinator
from .storage import ProxyStore

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]
type ProxyConfigEntry = ConfigEntry[ProxyCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ProxyConfigEntry) -> bool:
    coordinator = ProxyCoordinator(hass, entry)
    try:
        await coordinator.async_start()
    except (ValueError, KeyError, TypeError) as err:
        raise ConfigEntryError(
            "The Device Proxy checkpoint is missing or invalid. Restore it from backup."
        ) from err
    entry.runtime_data = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ProxyConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await ProxyStore(hass, entry).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 2:
        return False
    if entry.version == 1:
        store = ProxyStore(hass, entry)
        data = await store.async_load()
        if data is None:
            raise ConfigEntryError(
                "The Appliance Meter checkpoint is missing. Restore it from backup."
            )
        await store.async_save(data)
        # Retain historical selector entities, but retire the unreviewed write path.
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.domain == "select" and entity.unique_id.endswith("_assignment"):
                registry.async_update_entity(
                    entity.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                )
        hass.config_entries.async_update_entry(
            entry, data={"initialized": True}, options={}, version=2
        )
    return True
