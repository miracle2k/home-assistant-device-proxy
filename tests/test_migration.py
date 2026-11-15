"""Upgrade checkpoints and entity identities from Appliance Meter 0.1."""

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.appliance_meter.const import DOMAIN


@pytest.mark.parametrize("source_class", ["total", "total_increasing"])
async def test_v1_upgrade_keeps_totals_ids_assignment_and_baseline(
    hass, plugs, hass_storage, entity_registry, report, source_class
):
    config = {
        "appliances": [{"id": "dryer", "name": "Dryer"}],
        "meters": [
            {
                "id": f"meter-{number}",
                "name": f"Plug {number}",
                "device_id": plug["device_id"],
                "sources": plug["entities"],
            }
            for number, plug in enumerate(plugs, start=1)
        ],
        "metrics": ["energy", "power", "current", "voltage"],
    }
    entry = MockConfigEntry(
        domain=DOMAIN, title="Appliance Meter", data=config, version=1, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    original = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_dryer_energy",
        suggested_object_id="original_dryer_total",
        config_entry=entry,
    )
    old_select = entity_registry.async_get_or_create(
        "select",
        DOMAIN,
        f"{entry.entry_id}_meter-1_assignment",
        suggested_object_id="old_assignment",
        config_entry=entry,
    )
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {
            "totals": {"dryer": "2.5"},
            "meters": {
                "meter-1": {
                    "source": plugs[0]["entities"]["energy"]["registry_id"],
                    "appliance": "dryer",
                    "baseline": "100.5",
                    "last_seen": 0,
                    "assigned_at": 0,
                    "resets": 0,
                    "pending_reset": None,
                },
            },
        },
    }
    report("sensor.plug_1_energy", 101, state_class=source_class)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2
    assert entity_registry.async_get("sensor.original_dryer_total").id == original.id
    assert hass.states.get("sensor.original_dryer_total").state == "3.0"
    assert (
        entity_registry.async_get(old_select.entity_id).disabled_by
        == er.RegistryEntryDisabler.INTEGRATION
    )
    assert entry.runtime_data.binding("dryer")["device_id"] == plugs[0]["device_id"]
    report("sensor.plug_1_energy", "101.2")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.original_dryer_total").state == "3.2"
    await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["version"] == 2


@pytest.mark.parametrize("version", [1, 2])
async def test_missing_checkpoint_does_not_silently_start_over(hass, plugs, version):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"initialized": True}, version=version, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state == (
        ConfigEntryState.MIGRATION_ERROR
        if version == 1
        else ConfigEntryState.SETUP_ERROR
    )
