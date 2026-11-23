"""Paring med Nausto og valg av hvilke sensorer integrasjonen kan tilby."""

from pathlib import Path
import sqlite3
import time

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, area_registry, instance_id, selector

from .client import Client
from .control import MAX_CONTROLS, candidates as control_candidates, control_catalog, controllable, legacy_controls
from .const import DOMAIN, MAX_ENTITIES
from .entities import candidates, discovery_settings, snapshots
from .mapping import catalog, kind
from .profiles import Profiles, connection_id
from .pairing import read_pairing
from .store import encoded


LEGACY_KEYS = ("allow_control", "control_entities", "control_off_entities")


def now() -> int:
    return int(time.time() * 1000)


def form_schema(available, selected, scope, *, pairing=False, retry=False):
    # Sensorer, brytere og lys står i samme liste; ingen egen lov om bord for styring (ADR 310).
    fields = {}
    if pairing:
        fields[vol.Required("pairing_code")] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
    discover_all, discover_area = scope
    fields[vol.Required("discover_all", default=discover_all)] = selector.BooleanSelector()
    description = {"suggested_value": discover_area} if discover_area else None
    fields[vol.Optional("discover_area", description=description)] = selector.AreaSelector()
    options = [{"value": entity.registry_id, "label": f"{entity.device_name + ' · ' if entity.device_name else ''}{entity.name} ({entity.entity_id})"[:200]}
               for entity in sorted(available.values(), key=lambda item: (item.device_name, item.name, item.entity_id))]
    fields[vol.Required("entities", default=[value for value in selected if value in available])] = selector.SelectSelector(selector.SelectSelectorConfig(
        options=options, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN))
    if retry:
        fields[vol.Optional("retry_failed", default=False)] = selector.BooleanSelector()
    return vol.Schema(fields)


def validate_selection(connection, instance, sensors, controls, selected, required=True):
    """Kontroller valget før en engangskode eller et nytt oppsett tas i bruk.

    En ny tilkobling må dele noe; i alternativene kan eieren slutte å dele alt."""
    if (not isinstance(selected, list) or required and not selected or len(selected) > MAX_ENTITIES or len(set(selected)) != len(selected) or
            any(value not in sensors and value not in controls for value in selected)):
        raise ValueError("invalid_entities")
    if sum(value in controls for value in selected) > MAX_CONTROLS:
        raise ValueError("invalid_controls")
    shared = [sensors[value] for value in selected if value in sensors]
    scalar = catalog(connection, instance, shared)
    devices = {entity.device_id for entity in shared if entity.device_id}
    offer, _ = control_catalog(connection, instance, list(controllable(controls, set(selected), devices).values()), {})
    if len({source["id"] for source in scalar["sources"] + offer["sources"]}) > 16:
        raise ValueError("source_limit")


def selection_error(error):
    return str(error) if str(error) in ("invalid_entities", "invalid_controls") else "source_limit"


def scope_from_input(user_input):
    return user_input.get("discover_all") is True, user_input.get("discover_area") or None


def valid_scope(hass, scope):
    discover_all, area = scope
    return area is None or (
        not discover_all and isinstance(area, str) and area_registry.async_get(hass).async_get_area(area) is not None
    )


def sharing_options(selected, scope):
    return {"entities": selected, "discover_all": scope[0], "discover_area": scope[1]}


class NaustoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(_entry):
        return NaustoOptionsFlow()

    async def async_step_user(self, user_input=None):
        sensors = {entity.registry_id: entity for entity in candidates(self.hass).values()}
        controls = control_candidates(self.hass)
        available = {**sensors, **controls}
        errors, selected, scope = {}, [], (False, None)
        if self.source == config_entries.SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            selected = entry.options.get("entities", entry.data.get("entities", []))
            scope = discovery_settings(entry)
        if user_input is not None:
            selected = user_input.get("entities", [])
            scope = scope_from_input(user_input)
            instance = await instance_id.async_get(self.hass)
            if not valid_scope(self.hass, scope):
                errors["discover_area"] = "invalid_scope"
            # Kontroller valget og kildegrensen før en engangskode brukes.
            try:
                validate_selection("00000000-0000-4000-8000-000000000000", instance, sensors, controls, selected,
                                   required=not any(scope))
            except ValueError as error:
                errors["entities" if str(error) == "invalid_entities" else "base"] = selection_error(error)
        form = form_schema(available, selected if isinstance(selected, list) else [], scope, pairing=True)
        if user_input is not None and not errors:
            return await self._pair(user_input["pairing_code"].strip(), sharing_options(selected, scope), form, instance)
        return self.async_show_form(step_id="user", data_schema=form, errors=errors)

    async def _pair(self, code, options, form, instance):
        profiles = None
        try:
            client = Client(aiohttp_client.async_get_clientsession(self.hass))
            code, expected_connection = read_pairing(code, client.endpoint)
            existing = self._get_reauth_entry() if self.source == config_entries.SOURCE_REAUTH else None
            if existing is not None and expected_connection is not None and existing.data["connection_id"] != expected_connection:
                return self.async_show_form(step_id="user", data_schema=form, errors={"base": "wrong_boat"})
            profiles = await self.hass.async_add_executor_job(Profiles, Path(self.hass.config.path(".storage", DOMAIN)), instance)
            if code.startswith("nsc_") and await self.hass.async_add_executor_job(profiles.index.get, "pairing_retry_at", 0) > now():
                return self.async_show_form(step_id="user", data_schema=form, errors={"base": "pairing_rate_limited"})
            key = await self.hass.async_add_executor_job(profiles.prepare, code, now())
            receipt = await client.send(code, encoded({"schema_version": 1, "action": "pair", "producer_key": key}))
            if not receipt.ok:
                if receipt.status == 429 and code.startswith("nsc_"):
                    await self.hass.async_add_executor_job(profiles.index.set, "pairing_retry_at", now() + max(1, receipt.retry) * 1000)
                error = "pairing_rate_limited" if receipt.status == 429 and code.startswith("nsc_") else "cannot_connect" if receipt.status >= 500 else "pairing_failed"
                return self.async_show_form(step_id="user", data_schema=form, errors={"base": error})
            target = connection_id(receipt.data.get("connection_id"))
            if code.startswith("nsc_"):
                await self.hass.async_add_executor_job(profiles.index.set, "pairing_retry_at", 0)
            if expected_connection is not None and expected_connection != target:
                raise ValueError("wrong_connection")
            if existing is not None and existing.data["connection_id"] != target:
                return self.async_show_form(step_id="user", data_schema=form, errors={"base": "wrong_boat"})
            active = [entry.data["connection_id"] for entry in self._async_current_entries()]
            profile = await self.hass.async_add_executor_job(profiles.activate, target, key, now(), active)
            await self.hass.async_add_executor_job(profile.close)
            configured = await self.async_set_unique_id(target)
            data = {"connection_id": target, "instance_id": instance, **options}
            if existing is not None:
                self._abort_if_unique_id_mismatch()
            if previous := existing or configured:
                # Et nytt valg erstatter 0.3.0s lov om bord og de to kontrollistene (ADR 310).
                kept = {key: value for key, value in previous.data.items() if key not in LEGACY_KEYS}
                changed = self.hass.config_entries.async_update_entry(previous, data={**kept, **data}, options=options)
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
            return self.async_show_form(step_id="user", data_schema=form, errors={"base": category})
        except (OSError, sqlite3.Error):
            return self.async_show_form(step_id="user", data_schema=form, errors={"base": "local_state"})
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
            controls = control_candidates(self.hass)
            # Kretsene 0.3.0 tillot, står valgt i lista til eieren lagrer i ny versjon (ADR 310).
            selected = list(dict.fromkeys(self.config_entry.options.get("entities", self.config_entry.data["entities"]) +
                                          [value for value in legacy_controls(self.config_entry) if value in controls]))
            sensors = {entity.registry_id: entity for entity in candidates(self.hass).values()}
            sensors.update({entity.registry_id: entity for entity in snapshots(self.hass, selected, old) if kind(entity) is not None})
            available = {**sensors, **controls}
            status = await self.hass.async_add_executor_job(profile.status)
            retry = bool(status["rejected_batches"] or status["control"]["failure"] or await self.hass.async_add_executor_job(profile.get, "control_delivery_rejected", False))
            errors = {}
            scope = discovery_settings(self.config_entry)
            if user_input is not None:
                chosen = user_input.get("entities", [])
                scope = scope_from_input(user_input)
                if not valid_scope(self.hass, scope):
                    errors["discover_area"] = "invalid_scope"
                try:
                    validate_selection(self.config_entry.data["connection_id"], instance, sensors, controls, chosen, required=False)
                except ValueError as error:
                    errors["entities" if str(error) == "invalid_entities" else "base"] = selection_error(error)
                if not errors:
                    if user_input.get("retry_failed"):
                        await self.hass.async_add_executor_job(profile.retry)
                    return self.async_create_entry(title="", data=sharing_options(chosen, scope))
                selected = chosen if isinstance(chosen, list) else selected
            return self.async_show_form(step_id="init", data_schema=form_schema(available, selected, scope, retry=retry), errors=errors)
        finally:
            if profile is not None:
                await self.hass.async_add_executor_job(profile.close)
            await self.hass.async_add_executor_job(profiles.close)
