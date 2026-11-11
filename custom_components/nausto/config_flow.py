"""Vanlig HA-oppsett: velg entiteter og bruk den kortlivede Nausto-koden."""

from pathlib import Path
import sqlite3
import time

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, instance_id, selector

from .client import Client
from .control import candidates as control_candidates, control_catalog
from .const import DOMAIN, MAX_ENTITIES
from .entities import candidates, snapshots
from .mapping import catalog, kind
from .profiles import Profiles, connection_id
from .pairing import read_pairing
from .store import encoded


def now() -> int:
    return int(time.time() * 1000)


def form_schema(available, selected, pairing=False, retry=False, control_available=None, control_options=None):
    fields = {}
    if pairing:
        fields[vol.Required("pairing_code")] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
    options = [{"value": entity.registry_id, "label": f"{entity.device_name + ' · ' if entity.device_name else ''}{entity.name} ({entity.entity_id})"[:200]}
               for entity in sorted(available.values(), key=lambda item: (item.device_name, item.name, item.entity_id))]
    fields[vol.Required("entities", default=selected)] = selector.SelectSelector(selector.SelectSelectorConfig(
        options=options, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN))
    controls = control_available or {}
    values = control_options or {}
    choices = [{"value": item.registry_id, "label": f"{item.name} ({item.entity_id})"[:200]}
               for item in sorted(controls.values(), key=lambda value: (value.name, value.entity_id))]
    fields[vol.Optional("allow_control", default=values.get("allow_control", False))] = selector.BooleanSelector()
    for key in ("control_entities", "control_off_entities"):
        fields[vol.Optional(key, default=[value for value in values.get(key, []) if value in controls])] = selector.SelectSelector(selector.SelectSelectorConfig(
            options=choices, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN))
    if retry:
        fields[vol.Optional("retry_failed", default=False)] = selector.BooleanSelector()
    return vol.Schema(fields)


def control_selection(values, available, latches=None):
    if values.get("allow_control", False) is not True:
        return {"allow_control": False, "control_entities": [], "control_off_entities": []}
    chosen, off = values.get("control_entities", []), values.get("control_off_entities", [])
    if (not isinstance(chosen, list) or not isinstance(off, list) or not 1 <= len(chosen) <= 16 or
            len(set(chosen)) != len(chosen) or len(set(off)) != len(off) or
            any(value not in available for value in chosen) or any(value not in chosen for value in off) or
            any((latches or {}).get(value) is True for value in off)):
        raise ValueError("invalid_controls")
    return {"allow_control": True, "control_entities": chosen, "control_off_entities": off}


def validate_sources(connection, instance, available, selected, controls, values, latches=None):
    scalar = catalog(connection, instance, [available[value] for value in selected])
    if not values["allow_control"]:
        return
    offer, _ = control_catalog(connection, instance, [controls[value] for value in values["control_entities"]], values["control_off_entities"], latches or {})
    if len({source["id"] for source in scalar["sources"] + offer["sources"]}) > 16:
        raise ValueError("source_limit")


class NaustoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(_entry):
        return NaustoOptionsFlow()

    async def async_step_user(self, user_input=None):
        available = {entity.registry_id: entity for entity in candidates(self.hass).values()}
        controls = control_candidates(self.hass)
        errors, selected = {}, []
        if user_input is not None:
            selected = user_input.get("entities", [])
            if len(selected) > MAX_ENTITIES or len(set(selected)) != len(selected) or any(value not in available for value in selected):
                errors["entities"] = "invalid_entities"
            else:
                instance = await instance_id.async_get(self.hass)
                # Kontroller kildegrensen før en engangskode brukes.
                try:
                    options = control_selection(user_input, controls)
                    if not selected and not options["allow_control"]:
                        raise ValueError("invalid_entities")
                    validate_sources("00000000-0000-4000-8000-000000000000", instance, available, selected, controls, options)
                except ValueError as error:
                    errors["base"] = str(error) if str(error) in ("invalid_controls", "invalid_entities") else "source_limit"
                if not errors:
                    return await self._pair(user_input["pairing_code"].strip(), selected, available, instance, options)
        return self.async_show_form(step_id="user", data_schema=form_schema(available, selected, pairing=True, control_available=controls, control_options=user_input), errors=errors)

    async def _pair(self, code, selected, available, instance, control_options=None):
        profiles = None
        try:
            client = Client(aiohttp_client.async_get_clientsession(self.hass))
            code, expected_connection = read_pairing(code, client.endpoint)
            profiles = await self.hass.async_add_executor_job(Profiles, Path(self.hass.config.path(".storage", DOMAIN)), instance)
            if code.startswith("nsc_") and await self.hass.async_add_executor_job(profiles.index.get, "pairing_retry_at", 0) > now():
                return self.async_show_form(step_id="user", data_schema=form_schema(available, selected, pairing=True,
                    control_available=control_candidates(self.hass), control_options=control_options), errors={"base": "pairing_rate_limited"})
            key = await self.hass.async_add_executor_job(profiles.prepare, code, now())
            receipt = await client.send(code, encoded({"schema_version": 1, "action": "pair", "producer_key": key}))
            if not receipt.ok:
                if receipt.status == 429 and code.startswith("nsc_"):
                    await self.hass.async_add_executor_job(profiles.index.set, "pairing_retry_at", now() + max(1, receipt.retry) * 1000)
                error = "pairing_rate_limited" if receipt.status == 429 and code.startswith("nsc_") else "cannot_connect" if receipt.status >= 500 else "pairing_failed"
                return self.async_show_form(step_id="user", data_schema=form_schema(available, selected, pairing=True), errors={"base": error})
            target = connection_id(receipt.data.get("connection_id"))
            if code.startswith("nsc_"):
                await self.hass.async_add_executor_job(profiles.index.set, "pairing_retry_at", 0)
            if expected_connection is not None and expected_connection != target:
                raise ValueError("wrong_connection")
            existing = self._get_reauth_entry() if self.source == config_entries.SOURCE_REAUTH else None
            if existing is not None and existing.data["connection_id"] != target:
                return self.async_show_form(step_id="user", data_schema=form_schema(available, selected, pairing=True), errors={"base": "wrong_boat"})
            active = [entry.data["connection_id"] for entry in self._async_current_entries()]
            profile = await self.hass.async_add_executor_job(profiles.activate, target, key, now(), active)
            await self.hass.async_add_executor_job(profile.close)
            configured = await self.async_set_unique_id(target)
            options = {"entities": selected, **(control_options or {"allow_control": False, "control_entities": [], "control_off_entities": []})}
            data = {"connection_id": target, "instance_id": instance, **options}
            if existing is not None:
                self._abort_if_unique_id_mismatch()
            if previous := existing or configured:
                changed = self.hass.config_entries.async_update_entry(previous, data={**previous.data, **data}, options=options)
                # Oppdateringslytteren laster på nytt når data endres. Nøkkelen ligger privat
                # utenfor config entry og kan ha endret seg selv om selve skjemaet er likt.
                if not changed or not previous.update_listeners:
                    self.hass.config_entries.async_schedule_reload(previous.entry_id)
                return self.async_abort(reason="reauth_successful" if existing else "already_configured")
            return self.async_create_entry(title="Nausto", data=data)
        except ValueError as error:
            category = str(error)
            if category not in ("invalid_code", "wrong_connection", "instance_changed", "pairing_limit", "connection_limit"):
                category = "pairing_failed"
            return self.async_show_form(step_id="user", data_schema=form_schema(available, selected, pairing=True), errors={"base": category})
        except (OSError, sqlite3.Error):
            return self.async_show_form(step_id="user", data_schema=form_schema(available, selected, pairing=True), errors={"base": "local_state"})
        finally:
            if profiles is not None:
                await self.hass.async_add_executor_job(profiles.close)

    async def async_step_reauth(self, _data):
        return await self.async_step_user()


class NaustoOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        instance = await instance_id.async_get(self.hass)
        profiles = await self.hass.async_add_executor_job(Profiles, Path(self.hass.config.path(".storage", DOMAIN)), instance)
        profile = None
        try:
            profile = await self.hass.async_add_executor_job(profiles.open, self.config_entry.data["connection_id"])
            old = await self.hass.async_add_executor_job(profile.get, "entity_metadata", {})
            selected = self.config_entry.options.get("entities", self.config_entry.data["entities"])
            controls = control_candidates(self.hass)
            control_options = {key: self.config_entry.options.get(key, self.config_entry.data.get(key, default)) for key, default in
                               (("allow_control", False), ("control_entities", []), ("control_off_entities", []))}
            latches = await self.hass.async_add_executor_job(profile.get, "control_protected", {})
            available = {entity.registry_id: entity for entity in candidates(self.hass).values()}
            available.update({entity.registry_id: entity for entity in snapshots(self.hass, selected, old) if kind(entity) is not None})
            status = await self.hass.async_add_executor_job(profile.status)
            retry = bool(status["rejected_batches"] or status["control"]["failure"] or await self.hass.async_add_executor_job(profile.get, "control_delivery_rejected", False))
            errors = {}
            if user_input is not None:
                chosen = user_input.get("entities", [])
                if len(chosen) > MAX_ENTITIES or len(set(chosen)) != len(chosen) or any(value not in available for value in chosen):
                    errors["entities"] = "invalid_entities"
                else:
                    try:
                        values = control_selection(user_input, controls, latches)
                        validate_sources(self.config_entry.data["connection_id"], instance, available, chosen, controls, values, latches)
                    except ValueError as error:
                        errors["base"] = "invalid_controls" if str(error) == "invalid_controls" else "source_limit"
                    if not errors:
                        if user_input.get("retry_failed"):
                            await self.hass.async_add_executor_job(profile.retry)
                        return self.async_create_entry(title="", data={"entities": chosen, **values})
            return self.async_show_form(step_id="init", data_schema=form_schema(available, selected, retry=retry, control_available=controls, control_options=user_input or control_options), errors=errors)
        finally:
            if profile is not None:
                await self.hass.async_add_executor_job(profile.close)
            await self.hass.async_add_executor_job(profiles.close)
