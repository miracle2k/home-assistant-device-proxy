"""Real HA entities, source lifecycles, and atomic reviewed changes."""

from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.appliance_meter.const import INCREASE, MIRROR
from custom_components.appliance_meter.coordinator import DraftConflict
from custom_components.appliance_meter.model import AssignmentPlan
from custom_components.appliance_meter.sources import channel_from_source, describe


def assignment(router, target, source, mapping=None):
    device = deepcopy(router.devices[target])
    mapping = (
        mapping
        if mapping is not None
        else router.data["saved_mappings"][target].get(source, {})
    )
    return AssignmentPlan(
        device,
        source,
        {channel["id"]: mapping.get(channel["id"]) for channel in device["channels"]},
        router.revision,
    )


async def test_assignment_mirrors_existing_zero_readings_without_waiting_for_change(
    hass, entry, source_factory
):
    source = source_factory(
        "Idle plug",
        {
            "power": {
                "device_class": "power",
                "unit_of_measurement": "W",
                "state_class": "measurement",
                "value": 0,
            },
            "current": {
                "device_class": "current",
                "unit_of_measurement": "A",
                "state_class": "measurement",
                "value": 0,
            },
        },
    )
    channels = [
        channel_from_source(describe(hass, item["registry_id"]), channel_id=key)
        for key, item in source["entities"].items()
    ]
    router = entry.runtime_data
    await router.async_commit(
        AssignmentPlan(
            {"id": "idle", "name": "Idle appliance", "channels": channels},
            source["device_id"],
            {key: item["registry_id"] for key, item in source["entities"].items()},
            router.revision,
        )
    )
    await hass.async_block_till_done()
    for metric in ("power", "current"):
        state = hass.states.get(f"sensor.idle_appliance_{metric}")
        assert Decimal(state.state) == 0
        assert state.attributes["source_entity"] == f"sensor.idle_plug_{metric}"


async def test_reassignment_preserves_ids_and_separate_histories(
    hass, entry, plugs, report
):
    router = entry.runtime_data
    registry_id = er.async_get(hass).async_get("sensor.dryer_energy").id
    report("sensor.plug_1_energy", "100.3")
    report("sensor.plug_2_energy", "200.5")
    await hass.async_block_till_done()
    mapping = {key: value["registry_id"] for key, value in plugs[0]["entities"].items()}
    await router.async_commit(
        assignment(router, "dryer", plugs[0]["device_id"], mapping)
    )
    assert router.binding("dehumidifier") is None
    assert hass.states.get("sensor.dehumidifier_energy").state == "0.3"
    assert hass.states.get("sensor.dehumidifier_power").state == "unavailable"
    assert hass.states.get("sensor.dryer_power").state == "120"
    report("sensor.plug_1_energy", "100.5")
    report("sensor.plug_1_power", 2400)
    report("sensor.plug_1_voltage", 239)
    report("sensor.plug_2_energy", 205)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dryer_energy").state == "0.7"
    assert hass.states.get("sensor.dryer_power").state == "2400"
    assert er.async_get(hass).async_get("sensor.dryer_energy").id == registry_id
    await router.async_commit(
        assignment(router, "dehumidifier", plugs[0]["device_id"], mapping)
    )
    report("sensor.plug_1_energy", "100.6")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "0.4"
    assert hass.states.get("sensor.dryer_energy").state == "0.7"


async def test_offline_assignment_discards_unknown_interval(hass, entry, plugs, report):
    router = entry.runtime_data
    report("sensor.plug_1_energy", "unavailable")
    await hass.async_block_till_done()
    mapping = {key: value["registry_id"] for key, value in plugs[0]["entities"].items()}
    await router.async_commit(
        assignment(router, "dryer", plugs[0]["device_id"], mapping)
    )
    report("sensor.plug_1_energy", 120)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dryer_energy").state == "0"
    report("sensor.plug_1_energy", 121)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dryer_energy").state == "1"


async def test_same_assignment_confirmation_keeps_gap_baseline(
    hass, entry, plugs, report
):
    router = entry.runtime_data
    report("sensor.plug_1_energy", "unavailable")
    await hass.async_block_till_done()
    old_since = router.binding("dehumidifier")["since"]
    await router.async_commit(assignment(router, "dehumidifier", plugs[0]["device_id"]))
    report("sensor.plug_1_energy", 102)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "2"
    assert router.binding("dehumidifier")["since"] == old_since


async def test_partial_mapping_edit_keeps_unchanged_offline_counter(
    hass, entry, plugs, report
):
    router = entry.runtime_data
    report("sensor.plug_1_energy", "unavailable")
    await hass.async_block_till_done()
    plan = assignment(router, "dehumidifier", plugs[0]["device_id"])
    plan.mapping["power"] = None
    await router.async_commit(plan)
    report("sensor.plug_1_energy", 102)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "2"
    assert hass.states.get("sensor.dehumidifier_power").state == "unavailable"


async def test_restart_recovers_same_assignment_consumption(hass, entry, report):
    report("sensor.plug_1_energy", "100.123")
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    report("sensor.plug_1_energy", "100.456")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "0.456"
    assert hass.states.get("sensor.dehumidifier_source_device").state == "Plug 1"


async def test_units_are_normalized_before_counter_allocation(hass, entry, report):
    report("sensor.plug_1_energy", 100500, unit_of_measurement="Wh")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "0.5"
    report("sensor.plug_1_energy", 101, unit_of_measurement="kWh")
    await hass.async_block_till_done()
    assert Decimal(hass.states.get("sensor.dehumidifier_energy").state) == 1


async def test_source_rename_and_entity_id_reuse(hass, entry, report, plugs):
    registry = er.async_get(hass)
    registry.async_update_entity(
        "sensor.plug_1_energy", new_entity_id="sensor.renamed_energy"
    )
    await hass.async_block_till_done()
    hass.states.async_set(
        "sensor.renamed_energy",
        "101",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "1"
    registry.async_remove("sensor.renamed_energy")
    await hass.async_block_till_done()
    registry.async_get_or_create(
        "sensor",
        "test",
        "different-device-counter",
        suggested_object_id="renamed_energy",
        device_id=plugs[1]["device_id"],
    )
    report("sensor.renamed_energy", 5000)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "1"


async def test_bad_and_restored_reports_do_not_count(hass, entry, report):
    for value in ("unknown", "unavailable", "NaN", "Infinity", -1):
        report("sensor.plug_1_energy", value)
        await hass.async_block_till_done()
    report("sensor.plug_1_energy", 900, restored=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "0"
    report("sensor.plug_1_energy", 101, restored=False)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dehumidifier_energy").state == "1"


async def test_save_failure_rolls_back_mapping_assignment_and_new_channels(
    hass, entry, plugs
):
    router = entry.runtime_data
    before = router._snapshot()
    mapping = {key: value["registry_id"] for key, value in plugs[0]["entities"].items()}
    with (
        patch.object(
            router.store, "async_save", AsyncMock(side_effect=OSError("disk full"))
        ),
        pytest.raises(HomeAssistantError, match="previous assignment is kept"),
    ):
        await router.async_commit(
            assignment(router, "dryer", plugs[0]["device_id"], mapping)
        )
    assert router.data["assignments"] == before["assignments"]
    assert router.data["saved_mappings"] == before["saved_mappings"]
    assert router.revision == before["revision"]


async def test_stale_review_is_rejected(hass, entry, plugs):
    router = entry.runtime_data
    first = assignment(router, "dryer", None)
    stale = assignment(router, "dehumidifier", None)
    await router.async_commit(first)
    with pytest.raises(DraftConflict):
        await router.async_commit(stale)
    assert router.binding("dehumidifier")["device_id"] == plugs[0]["device_id"]


async def test_generic_temperature_binary_enum_and_multiple_counters(
    hass, entry, source_factory, report
):
    source = source_factory(
        "Probe",
        {
            "temperature": {
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "value": 20,
                "quality": "good",
            },
            "door": {"domain": "binary_sensor", "device_class": "door", "value": "off"},
            "status": {
                "device_class": "enum",
                "options": ["idle", "running"],
                "value": "idle",
            },
            "water": {
                "device_class": "water",
                "unit_of_measurement": "L",
                "state_class": "total_increasing",
                "value": 900,
            },
            "energy": {
                "device_class": "energy",
                "unit_of_measurement": "Wh",
                "state_class": "total_increasing",
                "value": 1000,
            },
        },
    )
    channels = [
        channel_from_source(describe(hass, item["registry_id"]), channel_id=key)
        for key, item in source["entities"].items()
    ]
    mapping = {key: item["registry_id"] for key, item in source["entities"].items()}
    router = entry.runtime_data
    await router.async_commit(
        AssignmentPlan(
            {"id": "room", "name": "Room", "channels": channels},
            source["device_id"],
            mapping,
            router.revision,
        )
    )
    await hass.async_block_till_done()
    report("sensor.probe_temperature", 70, unit_of_measurement="°F")
    report("binary_sensor.probe_door", "on")
    report("sensor.probe_status", "running")
    report("sensor.probe_water", 910)
    report("sensor.probe_energy", 1015)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.room_temperature").state) == pytest.approx(
        21.111111
    )
    assert hass.states.get("sensor.room_temperature").attributes["quality"] == "good"
    assert hass.states.get("binary_sensor.room_door").state == "on"
    assert hass.states.get("sensor.room_status").state == "running"
    assert hass.states.get("sensor.room_water").state == "10"
    assert hass.states.get("sensor.room_energy").state == "15"
    assert {channel["mode"] for channel in channels} == {MIRROR, INCREASE}


async def test_frequent_reports_keep_a_fixed_save_deadline(hass, entry, report):
    router = entry.runtime_data
    with patch.object(
        router.store, "async_delay_save", wraps=router.store.async_delay_save
    ) as save:
        for value in (101, 102, 103):
            report("sensor.plug_1_energy", value)
            await hass.async_block_till_done()
        assert save.call_count == 1
    assert router.ledger.total("dehumidifier:energy") == Decimal(3)


async def test_dates_timestamps_text_and_signed_net_counter(
    hass, entry, source_factory, report
):
    source = source_factory(
        "Meter",
        {
            "net": {
                "device_class": "energy",
                "unit_of_measurement": "kWh",
                "state_class": "total",
                "value": -10,
            },
            "date": {"device_class": "date", "value": "2026-01-02"},
            "timestamp": {
                "device_class": "timestamp",
                "value": "2026-01-02T10:00:00+00:00",
            },
            "text": {"value": "ready"},
        },
    )
    router = entry.runtime_data
    channels = [
        channel_from_source(describe(hass, item["registry_id"]), channel_id=key)
        for key, item in source["entities"].items()
    ]
    await router.async_commit(
        AssignmentPlan(
            {"id": "meter", "name": "Virtual meter", "channels": channels},
            source["device_id"],
            {key: item["registry_id"] for key, item in source["entities"].items()},
            router.revision,
        )
    )
    await hass.async_block_till_done()
    report("sensor.meter_net", -12)
    report("sensor.meter_date", "2026-01-03")
    report("sensor.meter_timestamp", "2026-01-03T10:00:00+00:00")
    report("sensor.meter_text", "running")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.virtual_meter_net").state == "-2"
    assert hass.states.get("sensor.virtual_meter_date").state == "2026-01-03"
    assert (
        hass.states.get("sensor.virtual_meter_timestamp").state
        == "2026-01-03T10:00:00+00:00"
    )
    assert hass.states.get("sensor.virtual_meter_text").state == "running"
    report("sensor.meter_timestamp", "2026-01-03T10:00:00")
    report("sensor.meter_date", "broken")
    report("sensor.meter_net", -11)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.virtual_meter_net").state == "-1"
    assert hass.states.get("sensor.virtual_meter_timestamp").state == "unavailable"
    assert hass.states.get("sensor.virtual_meter_date").state == "unavailable"
