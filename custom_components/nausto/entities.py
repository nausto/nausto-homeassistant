"""Smale lesinger av valgte HA-entiteter; registre og navneendringer beholdes lokale."""

from dataclasses import asdict

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry

from .mapping import Entity, instant, kind


def snapshots(hass: HomeAssistant, selected: list[str] | None = None, previous: dict | None = None) -> list[Entity]:
    registry = entity_registry.async_get(hass)
    devices = device_registry.async_get(hass)
    wanted = None if selected is None else set(selected)
    cached = previous or {}
    result = []
    for entry in registry.entities.values():
        if (wanted is not None and entry.id not in wanted) or entry.disabled_by is not None:
            continue
        if entry.domain not in ("sensor", "binary_sensor", "counter"):
            continue
        state = hass.states.get(entry.entity_id)
        old = cached.get(entry.id, {})
        attributes = state.attributes if state is not None else {}
        device = devices.async_get(entry.device_id) if entry.device_id else None
        result.append(Entity(
            registry_id=entry.id, entity_id=entry.entity_id,
            name=entry.name or entry.original_name or (state.name if state is not None else old.get("name", entry.entity_id)),
            device_id=entry.device_id,
            device_name=(device.name_by_user or device.name or "") if device else "",
            device_class=attributes.get("device_class", entry.original_device_class or old.get("device_class")),
            unit=attributes.get("unit_of_measurement", entry.unit_of_measurement or old.get("unit")),
            state=state.state if state is not None else None,
            updated_at=instant(state.last_updated) if state is not None else old.get("updated_at"),
            reported_at=instant(state.last_reported) if state is not None else old.get("reported_at"),
            restored=attributes.get("restored") is True or state is None,
        ))
    if wanted is not None:
        found = {entity.registry_id for entity in result}
        for registry_id in wanted - found:
            if registry_id in cached:
                old = cached[registry_id]
                result.append(Entity(**old, state=None, restored=True))
    return result


def metadata(entities: list[Entity]) -> dict:
    return {entity.registry_id: {key: value for key, value in asdict(entity).items() if key not in ("state", "restored")} for entity in entities}


def candidates(hass: HomeAssistant) -> dict[str, Entity]:
    return {entity.entity_id: entity for entity in snapshots(hass) if kind(entity) is not None}
