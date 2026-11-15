"""Real native flows: automatic suggestions, review, cancellation, and confirmation."""

import json
from copy import deepcopy

import pytest
import voluptuous_serialize
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.config_validation import custom_serializer

from custom_components.appliance_meter.const import DOMAIN, UNMAPPED


def defaults(result):
    return {key.schema: key.default() for key in result["data_schema"].schema}


async def create_review(hass, source, name="Room"):
    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"name": name, "device_id": source["device_id"]},
    )
    assert flow["step_id"] == "readings"
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], defaults(flow)
    )
    assert flow["step_id"] == "behaviour"
    return await hass.config_entries.flow.async_configure(
        flow["flow_id"], defaults(flow)
    )


async def options(hass, entry, action, target=None):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": action}
    )
    if target is not None:
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"target": target}
        )
    return flow


async def reassign_mapping(hass, entry, target, source):
    flow = await options(hass, entry, "reassign", target)
    assert flow["step_id"] == "device"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"device_id": source["device_id"]}
    )
    assert flow["step_id"] == "mapping"
    return flow


async def apply_review(hass, flow):
    assert flow["step_id"] == "confirm"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"confirm": True}
    )
    await hass.async_block_till_done()
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    return flow


async def test_new_device_without_energy_requires_final_confirmation(
    hass, source_factory
):
    source = source_factory(
        "Room probe",
        {
            "temperature": {
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "value": 20,
            },
            "door": {"domain": "binary_sensor", "device_class": "door", "value": "off"},
        },
    )
    flow = await create_review(hass, source)
    assert flow["step_id"] == "confirm"
    assert not hass.config_entries.async_entries(DOMAIN)
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"confirm": False}
    )
    assert flow["errors"] == {"base": "confirmation_required"}
    assert not hass.config_entries.async_entries(DOMAIN)
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"confirm": True}
    )
    await hass.async_block_till_done()
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    assert flow["result"].version == 2
    assert hass.states.get("sensor.room_temperature").state == "20"
    hass.states.async_set(
        "sensor.room_probe_temperature",
        "21",
        {
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.room_temperature").state == "21"
    assert hass.states.get("binary_sensor.room_door").state == "off"
    await hass.config_entries.async_unload(flow["result"].entry_id)


async def test_offline_sources_can_be_selected_from_registry_metadata(hass, plugs):
    for item in plugs[0]["entities"].values():
        hass.states.async_remove(item["entity_id"])
    flow = await create_review(hass, plugs[0])
    assert flow["step_id"] == "confirm"
    assert "offline" in flow["description_placeholders"]["review"]
    hass.config_entries.flow.async_abort(flow["flow_id"])
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_known_mapping_is_reviewed_and_cancel_is_nonmutating(hass, entry, plugs):
    router = entry.runtime_data
    before = router._snapshot()
    flow = await reassign_mapping(hass, entry, "dryer", plugs[1])
    assert defaults(flow)["mapping"] == {
        key: item["registry_id"] for key, item in plugs[1]["entities"].items()
    }
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    assert flow["step_id"] == "confirm"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"confirm": False}
    )
    assert flow["errors"]["base"] == "confirmation_required"
    hass.config_entries.options.async_abort(flow["flow_id"])
    assert router._snapshot() == before


async def test_native_mapping_and_handling_forms_serialize_for_frontend(
    hass, entry, plugs
):
    flow = await reassign_mapping(hass, entry, "dryer", plugs[0])
    schema = json.loads(
        json.dumps(
            voluptuous_serialize.convert(
                flow["data_schema"], custom_serializer=custom_serializer
            )
        )
    )
    fields = schema[0]["selector"]["object"]["fields"]
    assert len(fields) == 4
    for field in fields.values():
        assert field["label"]
        assert field["selector"]["select"]["options"][0]["value"] == UNMAPPED
    hass.config_entries.options.async_abort(flow["flow_id"])
    flow = await options(hass, entry, "create")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "New item", "device_id": plugs[0]["device_id"]}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    assert flow["step_id"] == "behaviour"
    schema = json.loads(
        json.dumps(
            voluptuous_serialize.convert(
                flow["data_schema"], custom_serializer=custom_serializer
            )
        )
    )
    assert len(schema[0]["selector"]["object"]["fields"]) == 4
    hass.config_entries.options.async_abort(flow["flow_id"])


async def test_different_plug_suggests_matches_but_never_applies_early(
    hass, entry, plugs
):
    router = entry.runtime_data
    flow = await reassign_mapping(hass, entry, "dryer", plugs[0])
    assert (
        defaults(flow)["mapping"]["energy"]
        == plugs[0]["entities"]["energy"]["registry_id"]
    )
    assert router.binding("dryer")["device_id"] == plugs[1]["device_id"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    assert flow["step_id"] == "confirm"
    assert "Dehumidifier" in flow["description_placeholders"]["review"]
    assert "will become unassigned" in flow["description_placeholders"]["review"]
    await apply_review(hass, flow)
    assert router.binding("dryer")["device_id"] == plugs[0]["device_id"]
    assert router.binding("dehumidifier") is None
    assert not hass.states.async_all("select")


async def test_missing_and_extra_readings_keep_existing_entities(
    hass, entry, source_factory, report
):
    report("sensor.plug_2_energy", 201)
    await hass.async_block_till_done()
    old_id = er.async_get(hass).async_get("sensor.dryer_energy").id
    replacement = source_factory(
        "Replacement",
        {
            "power": {
                "device_class": "power",
                "unit_of_measurement": "W",
                "state_class": "measurement",
                "value": 100,
            },
            "temperature": {
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "value": 20,
            },
        },
    )
    flow = await reassign_mapping(hass, entry, "dryer", replacement)
    mapping = defaults(flow)["mapping"]
    assert mapping["power"] == replacement["entities"]["power"]["registry_id"]
    assert mapping["energy"] == UNMAPPED
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"mapping": mapping}
    )
    assert flow["step_id"] == "extras"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"readings": ["sensor.replacement_temperature"]}
    )
    assert hass.states.get("sensor.dryer_temperature") is None
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    await apply_review(hass, flow)
    assert hass.states.get("sensor.dryer_energy").state == "1"
    assert er.async_get(hass).async_get("sensor.dryer_energy").id == old_id
    assert hass.states.get("sensor.dryer_voltage").state == "unavailable"
    report("sensor.replacement_temperature", 22)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dryer_temperature").state == "22"


async def test_ambiguous_matches_and_remembered_manual_mapping(
    hass, entry, source_factory
):
    first = source_factory(
        "Original probe",
        {
            key: {
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "value": 20,
            }
            for key in ("front", "back")
        },
    )
    second = source_factory(
        "Other probe",
        {
            key: {
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "state_class": "measurement",
                "value": 21,
            }
            for key in ("left", "right")
        },
    )
    flow = await options(hass, entry, "create")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "Probe item", "device_id": first["device_id"]}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    await apply_review(hass, flow)
    router = entry.runtime_data
    target_id = next(
        key for key, item in router.devices.items() if item["name"] == "Probe item"
    )
    flow = await reassign_mapping(hass, entry, target_id, second)
    assert set(defaults(flow)["mapping"].values()) == {UNMAPPED}
    channel_ids = [channel["id"] for channel in router.devices[target_id]["channels"]]
    explicit = dict(
        zip(
            channel_ids,
            [second["entities"][key]["registry_id"] for key in ("right", "left")],
            strict=True,
        )
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"mapping": explicit}
    )
    await apply_review(hass, flow)
    flow = await options(hass, entry, "unassign", target_id)
    await apply_review(hass, flow)
    flow = await reassign_mapping(hass, entry, target_id, second)
    assert defaults(flow)["mapping"] == explicit
    assert router.binding(target_id) is None
    hass.config_entries.options.async_abort(flow["flow_id"])


async def test_duplicate_source_and_incompatible_mapping_rejected(hass, entry, plugs):
    flow = await reassign_mapping(hass, entry, "dryer", plugs[0])
    mapping = defaults(flow)["mapping"]
    mapping["power"] = plugs[0]["entities"]["voltage"]["registry_id"]
    before = deepcopy(entry.runtime_data.data["assignments"])
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"mapping": mapping}
        )
    assert entry.runtime_data.data["assignments"] == before
    hass.config_entries.options.async_abort(flow["flow_id"])


async def test_source_removed_between_review_and_confirmation(hass, entry, plugs):
    flow = await reassign_mapping(hass, entry, "dryer", plugs[0])
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    er.async_get(hass).async_remove("sensor.plug_1_power")
    await hass.async_block_till_done()
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"confirm": True}
    )
    assert flow["errors"] == {"base": "source_changed"}
    assert entry.runtime_data.binding("dryer")["device_id"] == plugs[1]["device_id"]
    hass.config_entries.options.async_abort(flow["flow_id"])


async def test_stale_wizard_and_rename_preserve_history(hass, entry, plugs, report):
    report("sensor.plug_2_energy", 201)
    await hass.async_block_till_done()
    stale = await options(hass, entry, "unassign", "dryer")
    rename = await options(hass, entry, "rename", "dryer")
    rename = await hass.config_entries.options.async_configure(
        rename["flow_id"], {"name": "Tumble dryer"}
    )
    await apply_review(hass, rename)
    assert hass.states.get("sensor.dryer_energy").state == "1"
    assert hass.states.get("sensor.dryer_source_device").state == "Plug 2"
    assert entry.runtime_data.devices["dryer"]["name"] == "Tumble dryer"
    stale = await hass.config_entries.options.async_configure(
        stale["flow_id"], {"confirm": True}
    )
    assert stale["errors"] == {"base": "configuration_changed"}
    assert entry.runtime_data.binding("dryer")["device_id"] == plugs[1]["device_id"]
    hass.config_entries.options.async_abort(stale["flow_id"])


async def test_single_shared_integration(hass, entry):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert flow["type"] == FlowResultType.ABORT
    assert flow["reason"] == "single_instance_allowed"


async def test_switches_are_optional_and_added_only_after_mapping_confirmation(
    hass, entry, source_factory
):
    source = source_factory(
        "Controllable plug",
        {
            "power": {
                "device_class": "power",
                "unit_of_measurement": "W",
                "state_class": "measurement",
                "value": 0,
            },
            "relay": {
                "domain": "switch",
                "name": "On/off",
                "device_class": "outlet",
                "value": "off",
            },
        },
    )
    flow = await options(hass, entry, "create")
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "Another item", "device_id": source["device_id"]}
    )
    assert defaults(flow)["readings"] == ["sensor.controllable_plug_power"]
    hass.config_entries.options.async_abort(flow["flow_id"])

    flow = await reassign_mapping(hass, entry, "dehumidifier", source)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    assert flow["step_id"] == "extras"
    assert defaults(flow)["readings"] == []
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"readings": ["switch.controllable_plug_relay"]}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], defaults(flow)
    )
    assert "On/off control is enabled" in flow["description_placeholders"]["review"]
    assert hass.states.get("switch.dehumidifier_on_off") is None
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"confirm": False}
    )
    assert hass.states.get("switch.dehumidifier_on_off") is None
    await apply_review(hass, flow)
    assert hass.states.get("switch.dehumidifier_on_off").state == "off"
