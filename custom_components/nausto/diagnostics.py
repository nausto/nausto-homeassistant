"""Diagnostikk med uttrykkelig tillatt filliste av tellere, uten identitet eller måledata."""

from .const import VERSION


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    counters = await hass.async_add_executor_job(runtime.store.status)
    return {
        "version": VERSION,
        "offered_channels": len(runtime.channels),
        "selected_channels": len(runtime.selected),
        "local_failure": runtime.local_failure,
        "delivery_failure": runtime.failure,
        "discarded_profile_observations": await hass.async_add_executor_job(runtime.profiles.index.get, "discarded", 0),
        "remote_control": {"enabled": runtime.actuation.enabled, "protocol_supported": runtime.actuation.supported,
                           **await hass.async_add_executor_job(runtime.actuation.storage.status)},
        **counters,
    }
