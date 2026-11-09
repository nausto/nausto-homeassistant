"""Naustos egen Home Assistant-integrasjon; bare utgående, valgt sensordeling."""

from pathlib import Path
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import aiohttp_client, instance_id

from .client import Client
from .const import DOMAIN
from .profiles import Profiles
from .runtime import Runtime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    instance = await instance_id.async_get(hass)
    if instance != entry.data["instance_id"]:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="instance_changed")
    profiles = None
    store = None
    runtime = None
    try:
        profiles = await hass.async_add_executor_job(Profiles, Path(hass.config.path(".storage", DOMAIN)), instance)
        store = await hass.async_add_executor_job(profiles.open, entry.data["connection_id"])
        if not await hass.async_add_executor_job(store.get, "key"):
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="pairing_required")
        runtime = Runtime(hass, entry, profiles, store, Client(aiohttp_client.async_get_clientsession(hass)))
        entry.runtime_data = runtime
        await runtime.async_start()
        entry.async_on_unload(entry.add_update_listener(_options_updated))
        return True
    except BaseException:
        if runtime is not None:
            await runtime.async_stop()
        elif store is not None:
            await hass.async_add_executor_job(store.close)
        if profiles is not None and runtime is None:
            await hass.async_add_executor_job(profiles.close)
        raise


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(_hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry):
    # Bruk profilens opprinnelige binding også når en klonet HA fjerner gammelt oppsett.
    profiles = await hass.async_add_executor_job(Profiles, Path(hass.config.path(".storage", DOMAIN)), entry.data["instance_id"])
    try:
        last = not any(value.entry_id != entry.entry_id for value in hass.config_entries.async_entries(DOMAIN))
        await hass.async_add_executor_job(profiles.remove, entry.data["connection_id"], int(time.time() * 1000), last)
    finally:
        await hass.async_add_executor_job(profiles.close)
