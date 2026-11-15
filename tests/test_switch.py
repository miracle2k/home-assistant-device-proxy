"""Switch commands follow reviewed mappings and never reach an old source."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.appliance_meter.model import AssignmentPlan
from custom_components.appliance_meter.sources import channel_from_source, describe


@pytest.fixture
async def control(hass, entry, source_factory):
    sources = [
        source_factory(
            f"Relay {number}",
            {
                "relay": {
                    "domain": "switch",
                    "name": "On/off",
                    "device_class": "outlet",
                    "value": "off",
                }
            },
        )
        for number in (1, 2)
    ]
    router = entry.runtime_data
    channel = channel_from_source(
        describe(hass, sources[0]["entities"]["relay"]["registry_id"]),
        channel_id="relay",
    )
    target = {"id": "controlled", "name": "Controlled appliance", "channels": [channel]}
    await router.async_commit(
        AssignmentPlan(
            target,
            sources[0]["device_id"],
            {"relay": sources[0]["entities"]["relay"]["registry_id"]},
            router.revision,
        )
    )
    await hass.async_block_till_done()
    return {
        "sources": sources,
        "router": router,
        "target": target,
        "entity_id": "switch.controlled_appliance_on_off",
    }


def reassign(control, source):
    router = control["router"]
    return AssignmentPlan(
        deepcopy(control["target"]),
        source["device_id"] if source else None,
        {"relay": source["entities"]["relay"]["registry_id"] if source else None},
        router.revision,
    )


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_on_off_dispatch_preserves_context_and_waits_for_source_state(
    hass, control, report, hass_admin_user, service
):
    proxy = control["entity_id"]
    source = control["sources"][0]["entities"]["relay"]["entity_id"]
    initial = "off" if service == "turn_on" else "on"
    report(source, initial)
    await hass.async_block_till_done()
    assert hass.states.get(proxy).state == initial
    original_call = hass.services.async_call
    context = Context(user_id=hass_admin_user.id)
    with patch.object(type(hass.services), "async_call", AsyncMock()) as dispatch:
        await original_call(
            "switch", service, {"entity_id": proxy}, blocking=True, context=context
        )
        dispatch.assert_awaited_once_with(
            "switch", service, {"entity_id": source}, blocking=True, context=context
        )
    assert hass.states.get(proxy).state == initial
    report(source, "on" if service == "turn_on" else "off")
    await hass.async_block_till_done()
    assert hass.states.get(proxy).state != initial


async def test_commands_follow_new_source_and_preserve_virtual_identity(hass, control):
    registry = er.async_get(hass)
    identity = registry.async_get(control["entity_id"]).id
    second = control["sources"][1]
    await control["router"].async_commit(reassign(control, second))
    original_call = hass.services.async_call
    with patch.object(type(hass.services), "async_call", AsyncMock()) as dispatch:
        await original_call(
            "switch", "turn_on", {"entity_id": control["entity_id"]}, blocking=True
        )
        assert dispatch.await_args.args[2] == {
            "entity_id": second["entities"]["relay"]["entity_id"]
        }
    assert registry.async_get(control["entity_id"]).id == identity


@pytest.mark.parametrize(
    "condition",
    [
        "unassigned",
        "unmapped",
        "offline",
        "removed",
        "disabled",
        "incompatible",
        "reused",
    ],
)
async def test_unavailable_or_changed_sources_cannot_receive_commands(
    hass, control, report, condition
):
    router = control["router"]
    source = control["sources"][0]["entities"]["relay"]["entity_id"]
    registry = er.async_get(hass)
    if condition == "unassigned":
        await router.async_commit(reassign(control, None))
    elif condition == "unmapped":
        plan = reassign(control, control["sources"][0])
        plan.mapping["relay"] = None
        await router.async_commit(plan)
    elif condition == "offline":
        report(source, "unavailable")
    elif condition == "disabled":
        registry.async_update_entity(source, disabled_by=er.RegistryEntryDisabler.USER)
    elif condition == "incompatible":
        report(source, "off", device_class="switch")
    else:
        registry.async_remove(source)
        if condition == "reused":
            registry.async_get_or_create(
                "switch",
                "test",
                "unrelated-relay",
                suggested_object_id=source.split(".")[1],
                device_id=control["sources"][1]["device_id"],
            )
            report(source, "on")
    await hass.async_block_till_done()
    assert hass.states.get(control["entity_id"]).state == "unavailable"
    with patch.object(type(hass.services), "async_call", AsyncMock()) as dispatch:
        with pytest.raises(HomeAssistantError):
            await router.async_control_switch("controlled", "relay", "turn_on", None)
        dispatch.assert_not_awaited()


async def test_source_entity_rename_is_resolved_at_command_time(hass, control):
    source = control["sources"][0]["entities"]["relay"]["entity_id"]
    state = hass.states.get(source)
    er.async_get(hass).async_update_entity(source, new_entity_id="switch.renamed_relay")
    hass.states.async_set("switch.renamed_relay", "off", state.attributes)
    await hass.async_block_till_done()
    original_call = hass.services.async_call
    with patch.object(type(hass.services), "async_call", AsyncMock()) as dispatch:
        await original_call(
            "switch", "turn_on", {"entity_id": control["entity_id"]}, blocking=True
        )
        assert dispatch.await_args.args[2] == {"entity_id": "switch.renamed_relay"}


async def test_assignment_waits_for_in_flight_command(hass, control):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def dispatch(*args, **kwargs):
        started.set()
        await finish.wait()

    router = control["router"]
    with patch.object(
        type(hass.services), "async_call", AsyncMock(side_effect=dispatch)
    ):
        command = asyncio.create_task(
            router.async_control_switch("controlled", "relay", "turn_on", None)
        )
        await started.wait()
        change = asyncio.create_task(
            router.async_commit(reassign(control, control["sources"][1]))
        )
        try:
            await asyncio.sleep(0)
            assert not change.done()
            assert (
                router.binding("controlled")["device_id"]
                == control["sources"][0]["device_id"]
            )
        finally:
            finish.set()
            await asyncio.gather(command, change)
    assert (
        router.binding("controlled")["device_id"] == control["sources"][1]["device_id"]
    )
