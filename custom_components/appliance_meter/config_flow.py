"""Native create/reassign/review/confirm flows; no templates or model profiles."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MODES, NAME, UNMAPPED
from .coordinator import DraftConflict, validate_assignment
from .model import AssignmentPlan, check_name, initial_state
from .sources import (
    allowed_modes,
    channel_from_source,
    compatible,
    describe,
    device_name,
    discover,
    set_mode,
    source_state,
)


def label(info: dict[str, Any]) -> str:
    return f"{info['name']} · {info['unit'] or info['domain']} · {info['entity_id']}"


def safe(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")


def handling_label(channel: dict[str, Any], mode: str) -> str:
    if channel["domain"] == "switch":
        return "Mirror state and forward on/off"
    return MODES[mode]


class ReviewFlow:
    """Drafts remain local to a flow until the final checked confirmation."""

    _revision = 0
    _operation = "assign"
    _physical: str | None = None
    _new_ids: list[str]
    _target: dict[str, Any]
    _mapping: dict[str, str | None]
    _sources: dict[str, dict[str, Any]]

    def _name_error(self, name: str) -> str | None:
        try:
            check_name(name)
        except ValueError:
            return "invalid_name"
        if hasattr(self, "_snapshot") and any(
            device["id"] != self._target.get("id")
            and device["name"].casefold() == name.strip().casefold()
            for device in self._snapshot["devices"].values()
        ):
            return "duplicate_name"
        return None

    def _select(self, options, **kwargs):
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=value, label=text)
                    for value, text in options
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
                **kwargs,
            )
        )

    async def _new_device(
        self, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors = {}
        self._target = getattr(
            self, "_target", {"id": uuid4().hex, "name": "", "channels": []}
        )
        if user_input is not None:
            name = user_input["name"].strip()
            if error := self._name_error(name):
                errors["name"] = error
            elif dr.async_get(self.hass).async_get(user_input["device_id"]) is None:
                errors["device_id"] = "unknown_source"
            elif not (sources := discover(self.hass, user_input["device_id"])):
                errors["device_id"] = "no_readings"
            else:
                self._target["name"] = name
                self._physical = user_input["device_id"]
                self._sources = sources
                self._mapping = {}
                self._new_ids = []
                return await self.async_step_readings()
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required("name"): selector.TextSelector(),
                    vol.Required("device_id"): selector.DeviceSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_readings(self, user_input=None):
        errors = {}
        if user_input is not None:
            by_entity = {item["entity_id"]: item for item in self._sources.values()}
            selected = user_input.get("readings", [])
            if not selected:
                errors["readings"] = "choose_readings"
            elif len(selected) != len(set(selected)) or any(
                entity not in by_entity for entity in selected
            ):
                errors["readings"] = "invalid_mapping"
            else:
                self._target["channels"] = []
                self._new_ids = []
                self._mapping = {}
                for entity_id in selected:
                    info = by_entity[entity_id]
                    channel = channel_from_source(info)
                    self._target["channels"].append(channel)
                    self._new_ids.append(channel["id"])
                    self._mapping[channel["id"]] = info["registry_id"]
                return await self.async_step_behaviour()
        return self.async_show_form(
            step_id="readings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "readings",
                        default=[
                            info["entity_id"]
                            for info in self._sources.values()
                            if info["domain"] != "switch"
                        ],
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            include_entities=[
                                info["entity_id"] for info in self._sources.values()
                            ],
                            multiple=True,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            device_id = user_input["device_id"]
            if dr.async_get(self.hass).async_get(device_id) is None:
                errors["device_id"] = "unknown_source"
            elif not (sources := discover(self.hass, device_id)):
                errors["device_id"] = "no_readings"
            else:
                self._physical = device_id
                self._sources = sources
                self._new_ids = []
                self._suggest_mapping()
                return await self.async_step_mapping()
        field = (
            vol.Required("device_id", default=self._physical)
            if self._physical
            else vol.Required("device_id")
        )
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({field: selector.DeviceSelector()}),
            errors=errors,
        )

    def _suggest_mapping(self):
        remembered = (
            self._snapshot["saved_mappings"]
            .get(self._target["id"], {})
            .get(self._physical, {})
        )
        self._mapping = {}
        used = set()
        candidates = {}
        for channel in self._target["channels"]:
            key = channel["id"]
            if key in remembered:
                source_id = remembered[key]
                if source_id in self._sources and compatible(
                    channel, self._sources[source_id]
                ):
                    self._mapping[key] = source_id
                    used.add(source_id)
                else:
                    self._mapping[key] = None
            else:
                candidates[key] = [
                    source_id
                    for source_id, info in self._sources.items()
                    if compatible(channel, info)
                ]
        candidates = {
            key: [value for value in values if value not in used]
            for key, values in candidates.items()
        }
        for key, values in candidates.items():
            # Never use list order to resolve an ambiguous match.
            if (
                len(values) == 1
                and sum(values[0] in other for other in candidates.values()) == 1
            ):
                self._mapping[key] = values[0]
            else:
                self._mapping[key] = None

    async def async_step_mapping(self, user_input=None):
        errors = {}
        if user_input is not None:
            supplied = user_input.get("mapping", {})
            mapping = {}
            for channel in self._target["channels"]:
                source_id = supplied.get(channel["id"], UNMAPPED)
                if source_id == UNMAPPED:
                    mapping[channel["id"]] = None
                elif source_id not in self._sources or not compatible(
                    channel, self._sources[source_id]
                ):
                    errors["base"] = "invalid_mapping"
                else:
                    mapping[channel["id"]] = source_id
            selected = [value for value in mapping.values() if value]
            if len(selected) != len(set(selected)):
                errors["base"] = "duplicate_source"
            if not errors:
                self._mapping = mapping
                return await self.async_step_extras()
        fields = {}
        for channel in self._target["channels"]:
            choices = [(UNMAPPED, "Not mapped — keep the reading unavailable")]
            choices.extend(
                (key, label(info))
                for key, info in self._sources.items()
                if compatible(channel, info)
            )
            fields[channel["id"]] = {
                "label": f"{channel['name']} · {channel['unit'] or channel['domain']}",
                "required": True,
                "selector": self._select(choices),
            }
        defaults = {key: value or UNMAPPED for key, value in self._mapping.items()}
        return self.async_show_form(
            step_id="mapping",
            data_schema=vol.Schema(
                {
                    vol.Required("mapping", default=defaults): selector.ObjectSelector(
                        selector.ObjectSelectorConfig(fields=fields)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "target": self._target["name"],
                "source": device_name(self.hass, self._physical),
            },
        )

    async def async_step_extras(self, user_input=None):
        used = {value for value in self._mapping.values() if value}
        extras = {key: info for key, info in self._sources.items() if key not in used}
        if not extras:
            return await self.async_step_confirm()
        if user_input is not None:
            by_entity = {info["entity_id"]: info for info in extras.values()}
            for entity_id in user_input.get("readings", []):
                if entity_id not in by_entity:
                    return self.async_abort(reason="source_changed")
                info = by_entity[entity_id]
                channel = channel_from_source(info)
                self._target["channels"].append(channel)
                self._mapping[channel["id"]] = info["registry_id"]
                self._new_ids.append(channel["id"])
            if self._new_ids:
                return await self.async_step_behaviour()
            return await self.async_step_confirm()
        return self.async_show_form(
            step_id="extras",
            data_schema=vol.Schema(
                {
                    vol.Optional("readings", default=[]): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            include_entities=[
                                info["entity_id"] for info in extras.values()
                            ],
                            multiple=True,
                        )
                    ),
                }
            ),
        )

    async def async_step_behaviour(self, user_input=None):
        if not self._new_ids:
            return await self.async_step_confirm()
        fields = {}
        defaults = {}
        for channel in self._target["channels"]:
            if channel["id"] not in self._new_ids:
                continue
            fields[channel["id"]] = {
                "label": f"{channel['name']} · {channel['unit'] or channel['domain']}",
                "required": True,
                "selector": self._select(
                    [
                        (mode, handling_label(channel, mode))
                        for mode in allowed_modes(channel)
                    ]
                ),
            }
            defaults[channel["id"]] = channel["mode"]
        errors = {}
        if user_input is not None:
            try:
                modes = user_input["modes"]
                self._target["channels"] = [
                    set_mode(channel, modes[channel["id"]])
                    if channel["id"] in self._new_ids
                    else channel
                    for channel in self._target["channels"]
                ]
            except ValueError, KeyError:
                errors["base"] = "invalid_mode"
            else:
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="behaviour",
            data_schema=vol.Schema(
                {
                    vol.Required("modes", default=defaults): selector.ObjectSelector(
                        selector.ObjectSelectorConfig(fields=fields)
                    ),
                }
            ),
            errors=errors,
        )

    def _plan(self):
        return AssignmentPlan(
            deepcopy(self._target),
            self._physical,
            dict(self._mapping),
            self._revision,
            self._operation,
        )

    def _review(self):
        physical = (
            safe(device_name(self.hass, self._physical))
            if self._physical
            else "Unassigned"
        )
        lines = [
            f"**Virtual device:** {safe(self._target['name'])}",
            f"**Physical source:** {physical}",
            "",
        ]
        if hasattr(self, "_snapshot") and self._physical:
            for target_id, binding in self._snapshot["assignments"].items():
                if (
                    target_id != self._target["id"]
                    and binding["device_id"] == self._physical
                ):
                    lines += [
                        f"**{safe(self._snapshot['devices'][target_id]['name'])} "
                        "will become unassigned.** "
                        "Its history and totals will be kept.",
                        "",
                    ]
        lines += [
            "| Virtual reading | Source reading | Handling |",
            "| --- | --- | --- |",
        ]
        offline = False
        for channel in self._target["channels"]:
            source_id = self._mapping.get(channel["id"]) if self._physical else None
            info = describe(self.hass, source_id) if source_id else None
            source_label = (
                f"{info['name']} ({info['unit'] or info['domain']})"
                if info
                else "Unmapped"
            )
            state = source_state(self.hass, source_id)
            if source_id and (
                state is None
                or state.state in ("unknown", "unavailable")
                or state.attributes.get("restored")
            ):
                source_label += " — offline"
                offline = True
            unit = f" ({channel['unit']})" if channel["unit"] else ""
            lines.append(
                f"| {safe(channel['name'] + unit)} | {safe(source_label)} | "
                f"{safe(handling_label(channel, channel['mode']))} |"
            )
        if self._physical and any(
            channel["domain"] == "switch" and self._mapping.get(channel["id"])
            for channel in self._target["channels"]
        ):
            lines += [
                "",
                "**On/off control is enabled.** Operating a virtual switch controls "
                "its mapped switch on the physical source.",
            ]
        if offline:
            lines += [
                "",
                "**A source is offline.** Live readings wait for a fresh report. "
                "A newly assigned counter starts from its first valid reading; "
                "the unknown interval is not charged.",
            ]
        lines += [
            "",
            "The current assignment stays active until you confirm. "
            "Existing entity IDs, history, and totals are kept. Units are "
            "converted to the virtual reading's existing unit.",
        ]
        return "\n\n".join(lines[:3]) + "\n" + "\n".join(lines[3:])

    async def async_step_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            if not user_input.get("confirm"):
                errors["base"] = "confirmation_required"
            else:
                try:
                    return await self._apply_confirmed(self._plan())
                except DraftConflict:
                    errors["base"] = "configuration_changed"
                except ValueError as err:
                    errors["base"] = (
                        str(err)
                        if str(err)
                        in ("duplicate_name", "duplicate_source", "invalid_name")
                        else "source_changed"
                    )
                except HomeAssistantError:
                    errors["base"] = "save_failed"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("confirm", default=False): selector.BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={"review": self._review()},
            last_step=True,
        )


class DeviceProxyConfigFlow(ReviewFlow, ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._new_device("user", user_input)

    async def _apply_confirmed(self, plan):
        # Revalidate the selected registry identities immediately before creation.
        from .model import empty_state

        validate_assignment(self.hass, empty_state(), plan)
        return self.async_create_entry(
            title=NAME,
            data={
                "initial": initial_state(plan, dt_util.utcnow().timestamp()),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return DeviceProxyOptionsFlow()


class DeviceProxyOptionsFlow(ReviewFlow, OptionsFlow):
    async def async_step_init(self, user_input=None):
        if self.config_entry.state != ConfigEntryState.LOADED:
            return self.async_abort(reason="not_ready")
        self._runtime = self.config_entry.runtime_data
        self._snapshot = self._runtime._snapshot()
        self._revision = self._snapshot["revision"]
        return self.async_show_menu(
            step_id="init", menu_options=["reassign", "create", "unassign", "rename"]
        )

    async def async_step_create(self, user_input=None):
        self._operation = "assign"
        return await self._new_device("create", user_input)

    async def async_step_reassign(self, user_input=None):
        self._operation = "assign"
        return await self.async_step_target()

    async def async_step_unassign(self, user_input=None):
        self._operation = "unassign"
        return await self.async_step_target()

    async def async_step_rename(self, user_input=None):
        self._operation = "rename"
        return await self.async_step_target()

    async def async_step_target(self, user_input=None):
        if user_input is not None:
            self._target = deepcopy(self._snapshot["devices"][user_input["target"]])
            binding = self._snapshot["assignments"].get(self._target["id"])
            self._physical = binding["device_id"] if binding else None
            self._mapping = {
                channel["id"]: binding["mapping"].get(channel["id"])
                if binding
                else None
                for channel in self._target["channels"]
            }
            self._new_ids = []
            if self._operation == "unassign":
                self._physical = None
                self._mapping = dict.fromkeys(self._mapping)
                return await self.async_step_confirm()
            if self._operation == "rename":
                return await self.async_step_name()
            return await self.async_step_device()
        return self.async_show_form(
            step_id="target",
            data_schema=vol.Schema(
                {
                    vol.Required("target"): self._select(
                        [
                            (key, device["name"])
                            for key, device in self._snapshot["devices"].items()
                        ]
                    ),
                }
            ),
        )

    async def async_step_name(self, user_input=None):
        errors = {}
        if user_input is not None:
            if error := self._name_error(user_input["name"]):
                errors["name"] = error
            else:
                self._target["name"] = user_input["name"].strip()
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "name", default=self._target["name"]
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def _apply_confirmed(self, plan):
        await self._runtime.async_commit(plan)
        # Configuration lives in the same checkpoint as the counters. Do not reload.
        return self.async_create_entry(title="", data=dict(self.config_entry.options))
