"""Isolated physical sources and real Home Assistant lifecycle fixtures."""

from copy import deepcopy

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.appliance_meter.const import DOMAIN
from custom_components.appliance_meter.model import empty_state
from custom_components.appliance_meter.sources import channel_from_source, describe


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    pass


@pytest.fixture
def source_factory(hass, device_registry, entity_registry):
    def create(name, specs):
        source_entry = MockConfigEntry(domain="test", unique_id=name)
        source_entry.add_to_hass(hass)
        device = device_registry.async_get_or_create(
            config_entry_id=source_entry.entry_id,
            identifiers={("test", name)},
            name=name,
        )
        result = {"device_id": device.id, "entities": {}}
        for key, spec in specs.items():
            attrs = {
                key: value
                for key, value in spec.items()
                if key not in ("value", "domain", "name")
            }
            entity = entity_registry.async_get_or_create(
                spec.get("domain", "sensor"),
                "test",
                f"{name}-{key}",
                suggested_object_id=f"{name.lower().replace(' ', '_')}_{key}",
                config_entry=source_entry,
                device_id=device.id,
                original_name=spec.get("name", key.replace("_", " ").capitalize()),
                original_device_class=attrs.get("device_class"),
                unit_of_measurement=attrs.get("unit_of_measurement"),
                capabilities={
                    key: value
                    for key, value in attrs.items()
                    if key in ("state_class", "options")
                },
            )
            hass.states.async_set(entity.entity_id, str(spec["value"]), attrs)
            result["entities"][key] = {
                "registry_id": entity.id,
                "entity_id": entity.entity_id,
            }
        return result

    return create


@pytest.fixture
def plugs(source_factory):
    return [
        source_factory(
            f"Plug {number}",
            {
                "energy": {
                    "device_class": "energy",
                    "unit_of_measurement": "kWh",
                    "state_class": "total_increasing",
                    "value": 100 * number,
                },
                "power": {
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "state_class": "measurement",
                    "value": 120,
                },
                "current": {
                    "device_class": "current",
                    "unit_of_measurement": "A",
                    "state_class": "measurement",
                    "value": 0.5,
                },
                "voltage": {
                    "device_class": "voltage",
                    "unit_of_measurement": "V",
                    "state_class": "measurement",
                    "value": 240,
                },
            },
        )
        for number in (1, 2)
    ]


@pytest.fixture
def config(hass, plugs):
    data = empty_state()
    for target_id, name, plug in zip(
        ("dehumidifier", "dryer"), ("Dehumidifier", "Dryer"), plugs, strict=True
    ):
        channels = [
            channel_from_source(describe(hass, source["registry_id"]), channel_id=key)
            for key, source in plug["entities"].items()
        ]
        data["devices"][target_id] = {
            "id": target_id,
            "name": name,
            "channels": channels,
        }
        mapping = {
            key: source["registry_id"] for key, source in plug["entities"].items()
        }
        data["assignments"][target_id] = {
            "device_id": plug["device_id"],
            "mapping": mapping,
            "since": 0,
        }
        data["saved_mappings"][target_id] = {plug["device_id"]: dict(mapping)}
    data["revision"] = 1
    return data


@pytest.fixture
async def entry(hass, config):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Proxy",
        data={"initial": deepcopy(config)},
        version=2,
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def report(hass):
    def update(entity_id, value, **attributes):
        state = hass.states.get(entity_id)
        hass.states.async_set(entity_id, str(value), {**state.attributes, **attributes})

    return update
