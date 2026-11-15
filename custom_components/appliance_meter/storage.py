"""Versioned storage shared by configuration, assignments, and counters."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .model import migrate_legacy
from .sources import describe


class ProxyStore(Store[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.entry = entry

    def legacy_state(self, checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        state = migrate_legacy(dict(self.entry.options or self.entry.data), checkpoint)
        for target_id, target in state["devices"].items():
            binding = state["assignments"].get(target_id)
            mappings = state["saved_mappings"].get(target_id, {}).values()
            if binding:
                mappings = [binding["mapping"], *mappings]
            for channel in target["channels"]:
                if channel["id"] != "energy":
                    continue
                for mapping in mappings:
                    source_id = mapping.get("energy")
                    info = describe(self.hass, source_id) if source_id else None
                    if info and info["input_state_class"] in (
                        "total",
                        "total_increasing",
                    ):
                        channel["input_state_class"] = info["input_state_class"]
                        break
        return state

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        if old_major_version == 1 and self.entry.version == 1:
            return self.legacy_state(old_data)
        raise ValueError("Unsupported Device Proxy checkpoint version")
