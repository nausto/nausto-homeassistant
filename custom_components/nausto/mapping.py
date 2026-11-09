"""Registeridentitet og enheter bestemmer betydningen; navn brukes bare som etiketter."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any
from uuid import UUID, uuid5

from .const import MAX_ENTITIES, MAX_SOURCES


@dataclass(frozen=True)
class Entity:
    """Et avgrenset øyeblikksbilde fra HAs registre og tilstandsmaskin."""

    registry_id: str
    entity_id: str
    name: str
    device_id: str | None
    device_name: str
    device_class: str | None
    unit: str | None
    state: str | None
    updated_at: str | None
    reported_at: str | None
    restored: bool = False


def instant(value: Any) -> str | None:
    """Kanonisk UTC med millisekunder; ingen lokal klokke brukes som kildeklokke."""
    if not isinstance(value, (str, datetime)):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


# Bare dokumenterte dimensjoner. Energi, trykk og volum krever egne kontraktstørrelser.
UNITS = {
    "V": ("voltage", "V", 1.0, 0.0),
    "mV": ("voltage", "V", 0.001, 0.0),
    "A": ("current", "A", 1.0, 0.0),
    "mA": ("current", "A", 0.001, 0.0),
    "W": ("power", "W", 1.0, 0.0),
    "kW": ("power", "W", 1000.0, 0.0),
    "C": ("charge", "C", 1.0, 0.0),
    "Ah": ("charge", "C", 3600.0, 0.0),
    "mAh": ("charge", "C", 3.6, 0.0),
    "K": ("temperature", "K", 1.0, 0.0),
    "°C": ("temperature", "K", 1.0, 273.15),
    "°F": ("temperature", "K", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    "%": ("ratio", "1", 0.01, 0.0),
    "m/s": ("speed", "m/s", 1.0, 0.0),
    "km/h": ("speed", "m/s", 1.0 / 3.6, 0.0),
    "kn": ("speed", "m/s", 1852.0 / 3600.0, 0.0),
    "mph": ("speed", "m/s", 1609.344 / 3600.0, 0.0),
    "s": ("duration", "s", 1.0, 0.0),
    "min": ("duration", "s", 60.0, 0.0),
    "h": ("duration", "s", 3600.0, 0.0),
    "d": ("duration", "s", 86400.0, 0.0),
}
CLASSES = {
    "voltage": "voltage", "current": "current", "power": "power",
    "temperature": "temperature", "battery": "ratio", "humidity": "ratio",
    "speed": "speed", "wind_speed": "speed", "duration": "duration",
}
ALARMS = {"moisture", "smoke", "gas", "carbon_monoxide", "problem", "safety", "tamper", "heat", "battery"}


def label(value: str) -> str:
    # Den delte TypeScript-valideringen teller UTF-16-enheter, også for emoji i HA-navn.
    return value.encode("utf-16-le", errors="ignore")[:200].decode("utf-16-le", errors="ignore") or "Home Assistant"


def kind(entity: Entity) -> tuple[str, str, str] | None:
    if not isinstance(entity.device_class, (str, type(None))) or not isinstance(entity.unit, (str, type(None))):
        return None
    domain = entity.entity_id.partition(".")[0]
    if domain == "binary_sensor":
        return ("alarm" if entity.device_class in ALARMS else "state", "none", "derived")
    if domain != "sensor":
        return None
    if entity.device_class == "enum":
        return ("state", "none", "derived")
    unit = UNITS.get(entity.unit or "")
    if unit is None:
        return None
    quantity, canonical, _, _ = unit
    expected = CLASSES.get(entity.device_class or "")
    # En ukjent eller motstridende device_class får ingen navnebasert gjetning.
    if entity.device_class and expected != quantity:
        return None
    provenance = "estimated" if entity.device_class == "battery" else "derived"
    return quantity, canonical, provenance


def catalog(connection_id: str, instance_id: str, entities: list[Entity]) -> dict:
    if len(entities) > MAX_ENTITIES or len({e.registry_id for e in entities}) != len(entities):
        raise ValueError("entity_limit")
    namespace = UUID(connection_id)
    sources = {}
    channels = []
    for entity in sorted(entities, key=lambda e: e.registry_id):
        semantic = kind(entity)
        if semantic is None or not entity.registry_id:
            continue
        quantity, unit, provenance = semantic
        source_external = f"home-assistant:{instance_id}:" + (f"device:{entity.device_id}" if entity.device_id else "virtual")
        source_id = str(uuid5(namespace, "source:" + source_external))
        source_name = label(entity.device_name or "Home Assistant")
        sources[source_id] = {"id": source_id, "external_id": source_external, "name": source_name, "vendor": "Home Assistant"}
        external = f"entity:{entity.registry_id}:{quantity}:{unit}:{provenance}"
        channel_id = str(uuid5(namespace, f"channel:{source_id}:{external}"))
        name = entity.name if not entity.device_name else f"{entity.device_name} · {entity.name}"
        channels.append({
            "id": channel_id, "source_id": source_id, "external_id": external,
            "name": label(name), "quantity": quantity, "unit": unit, "provenance": provenance,
            "_registry_id": entity.registry_id,
        })
    if len(sources) > MAX_SOURCES:
        raise ValueError("source_limit")
    return {"sources": list(sources.values()), "channels": channels}


def discovery(value: dict) -> dict:
    return {"schema_version": 1, "action": "discover", "sources": value["sources"],
            "channels": [{k: v for k, v in channel.items() if not k.startswith("_")} for channel in value["channels"]]}


def reading(entity: Entity, channel: dict) -> dict | None:
    if entity.restored:
        return None
    updated, reported = instant(entity.updated_at), instant(entity.reported_at)
    # En gammel cache uten kildeklokke gjøres aldri til et nytt opptak.
    if updated is None or reported is None or reported < updated:
        return None
    quality, value = "good", None
    if entity.state is None or entity.state == "unavailable":
        quality = "unavailable"
    elif entity.state == "unknown":
        quality = "unknown"
    elif channel["quantity"] == "alarm":
        if entity.state in ("on", "off"):
            value = entity.state == "on"
        else:
            quality = "unknown"
    elif channel["quantity"] == "state":
        if isinstance(entity.state, str) and 0 < len(entity.state.encode("utf-16-le", errors="surrogatepass")) // 2 <= 64:
            value = entity.state
        else:
            quality = "unknown"
    else:
        conversion = UNITS.get(entity.unit or "")
        try:
            number = float(entity.state)
            if conversion is None or conversion[0] != channel["quantity"] or not math.isfinite(number):
                raise ValueError("unknown_unit")
            value = number * conversion[2] + conversion[3]
            if not math.isfinite(value) or abs(value) > 1e12:
                raise ValueError("outside_contract")
        except (TypeError, ValueError, OverflowError):
            quality = "unknown"
    return {"channel_id": channel["id"], "source_id": channel["source_id"],
            "observed_at": updated, "confirmed_at": reported,
            "value": value if quality == "good" else None, "quality": quality}
