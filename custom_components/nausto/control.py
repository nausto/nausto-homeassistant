"""Uttrykkelig lokal styring: noncebundet lease, ett varig forsøk og separat kildebelegg."""

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from uuid import UUID, uuid4, uuid5
from homeassistant.core import callback

from .mapping import instant, label
from .const import CONTROL_BYTES
from .store import encoded, measurement_id, milliseconds, stamp

COMMAND_KEYS = {"id", "actuator_id", "source_id", "external_id", "actuator_version", "kind", "level_steps", "critical", "requested_at", "expires_at", "target"}
DIM_MODES = {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
MAX_ATTEMPTS = 128


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _time(value):
    try:
        return isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value) and stamp(milliseconds(value)) == value
    except (ValueError, OverflowError):
        return False


def read_commands(data, nonce, elapsed_ms):
    """Samme felt og regler som sensor-control-contract.ts; aldri en cached positiv lease."""
    if (not isinstance(data, dict) or data.get("control_protocol") != 1 or
            not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce) or
            data.get("control_request_nonce") != nonce or not _time(data.get("control_server_time")) or
            not isinstance(elapsed_ms, (int, float)) or not math.isfinite(elapsed_ms) or elapsed_ms < 0):
        return None
    commands = data.get("control_commands")
    if not isinstance(commands, list) or len(commands) > 16:
        return None
    ids, actuators, result = set(), set(), []
    server = milliseconds(data["control_server_time"])
    for command in commands:
        if not isinstance(command, dict) or set(command) != COMMAND_KEYS:
            return None
        if (not all(_uuid(command[key]) for key in ("id", "actuator_id", "source_id")) or
                command["id"] in ids or command["actuator_id"] in actuators or
                not isinstance(command["external_id"], str) or not command["external_id"].strip() or len(command["external_id"]) > 200 or
                type(command["actuator_version"]) is not int or not 1 <= command["actuator_version"] <= 2147483646 or
                command["kind"] not in ("switch", "dimmer") or type(command["critical"]) is not bool or
                not _time(command["requested_at"]) or not _time(command["expires_at"])):
            return None
        steps, target = command["level_steps"], command["target"]
        if (command["kind"] == "switch" and steps is not None or command["kind"] == "dimmer" and
                (type(steps) is not int or not 1 <= steps <= 10000)):
            return None
        if not isinstance(target, dict) or set(target) != {"on", "level"} or type(target["on"]) is not bool:
            return None
        level = target["level"]
        if level is not None and (type(level) not in (int, float) or not math.isfinite(level) or not 0 <= level <= 1):
            return None
        if (target["on"] and level == 0 or not target["on"] and level not in (None, 0) or
                command["critical"] and not target["on"] or command["kind"] == "switch" and level is not None):
            return None
        if command["kind"] == "dimmer" and target["on"] and level is not None:
            scaled = level * steps
            if scaled < 1 or abs(scaled - round(scaled)) > 1e-6:
                return None
        start, end = milliseconds(command["requested_at"]), milliseconds(command["expires_at"])
        remaining = end - server - elapsed_ms
        if start > server or remaining <= 0 or not 0 < end - start <= 60_000:
            return None
        ids.add(command["id"])
        actuators.add(command["actuator_id"])
        result.append({**command, "target": dict(target), "remaining_ms": remaining})
    return result


@dataclass(frozen=True)
class ControlEntity:
    registry_id: str
    entity_id: str
    device_id: str | None
    name: str
    device_name: str
    kind: str
    steps: int | None


def candidates(hass):
    from homeassistant.helpers import device_registry, entity_registry
    devices, registry = device_registry.async_get(hass), entity_registry.async_get(hass)
    result = {}
    for entry in registry.entities.values():
        if entry.disabled_by is not None or entry.domain not in ("switch", "light"):
            continue
        if not all(hass.services.has_service(entry.domain, service) for service in ("turn_on", "turn_off")):
            continue
        state = hass.states.get(entry.entity_id)
        if state is None:
            continue
        modes = state.attributes.get("supported_color_modes", [])
        dimmer = entry.domain == "light" and isinstance(modes, (list, tuple, set)) and bool(DIM_MODES.intersection(modes))
        device = devices.async_get(entry.device_id) if entry.device_id else None
        result[entry.id] = ControlEntity(entry.id, entry.entity_id, entry.device_id,
            label(entry.name or entry.original_name or state.name),
            label(device.name_by_user or device.name or "") if device else "",
            "dimmer" if dimmer else "switch", 255 if dimmer else None)
    return result


def control_catalog(connection, instance, entities, allowed_off, latches):
    sources, actuators = {}, []
    namespace = UUID(connection)
    updated = dict(latches)
    for entity in sorted(entities, key=lambda value: value.registry_id):
        external = f"home-assistant:{instance}:" + (f"device:{entity.device_id}" if entity.device_id else "virtual")
        source_id = str(uuid5(namespace, "source:" + external))
        sources[source_id] = {"id": source_id, "external_id": external, "name": entity.device_name or "Home Assistant", "vendor": "Home Assistant"}
        actuator_external = f"entity:{entity.registry_id}:control:{entity.kind}:{entity.steps}"
        protected = latches.get(entity.registry_id) is True or entity.registry_id not in allowed_off
        updated[entity.registry_id] = protected
        actuators.append({"id": str(uuid5(namespace, f"actuator:{source_id}:{actuator_external}")),
            "source_id": source_id, "external_id": actuator_external, "name": entity.name,
            "kind": entity.kind, "level_steps": entity.steps, "protected": protected})
    if len(sources) > 16 or len(actuators) > 16:
        raise ValueError("control_limit")
    return {"schema_version": 1, "action": "control_offer", "enabled": True,
            "sources": list(sources.values()), "actuators": actuators}, updated


class ControlStore:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript("""
              create table if not exists control_attempts(command_id text primary key,fingerprint text not null,
                attempt_id text not null,expires_at text not null,message text not null,pending integer not null default 1);
              create table if not exists control_reports(actuator_id text primary key,message text not null);
              create table if not exists control_outbox(kind text primary key check(kind in ('state','result')),message text not null,ids text not null);
            """)

    def size_bytes(self):
        # Teller faktiske UTF-8-bytes, også dupliserte forseglede resultater og metadata.
        total = 0
        for table in ("control_attempts", "control_reports", "control_outbox"):
            for row in self.store.db.execute("select * from " + table):
                total += sum(len(str(value).encode()) for value in row)
        total += self.store.db.execute("select coalesce(sum(length(cast(key as blob))+length(cast(value as blob))),0) from metadata where key like 'control_%'").fetchone()[0]
        return total

    def check_budget(self, reserve_result=True):
        total = self.size_bytes()
        # Små feil-/fairnessflagg kan settes uten å ta lagring fra started-markørene.
        if total + (4096 if reserve_result else 0) > CONTROL_BYTES - 512:
            raise ValueError("control_byte_limit")
        return total

    def status(self):
        with self.store.lock:
            state = self.store.db.execute("select ids from control_outbox where kind='state'").fetchone()
            return {"started_attempts": self.store.db.execute("select count(*) from control_attempts").fetchone()[0],
                    "pending_results": self.store.db.execute("select count(*) from control_attempts where pending=1").fetchone()[0],
                    "state_reports": self.store.db.execute("select count(*) from control_reports").fetchone()[0] + (len(json.loads(state[0])) if state else 0),
                    "bytes": self.size_bytes(), "delivery_rejected": self.store.get("control_delivery_rejected", False)}

    def configure(self, offer, latches):
        with self.store.transaction():
            self._reconcile({item["id"] for item in offer["actuators"]})
            self.store.set("control_protected", latches)
            self.store.set("control_was_enabled", True)
            self.store.set("control_current_offer", encoded(offer))
            self.check_budget()

    def _reconcile(self, allowed):
        for row in self.store.db.execute("select actuator_id from control_reports").fetchall():
            if row[0] not in allowed:
                self.store.db.execute("delete from control_reports where actuator_id=?", (row[0],))
                self.store._gap(1, int(time.time() * 1000))
        row = self.store.db.execute("select message,ids from control_outbox where kind='state'").fetchone()
        if row and any(value not in allowed for value in json.loads(row[1])):
            # Forseglede bytes omskrives aldri ved fravalg; hele meldingen gir et datagap.
            self.store._gap(len(json.loads(row[1])), int(time.time() * 1000))
            self.store.db.execute("delete from control_outbox where kind='state'")

    def disable(self):
        with self.store.transaction():
            self._reconcile(set())

    def mark_offered(self, message):
        with self.store.transaction():
            self.store.set("control_offered", message)
            self.check_budget()

    def verified_time(self, server_time):
        with self.store.transaction():
            # Kalles bare etter NY noncebundet HTTP-respons. En gyldig started-markør
            # evikteres aldri for å frigjøre plass. Utgåtte, ubesvarte resultater gir gap.
            cutoff = stamp(milliseconds(server_time) - 300_000)
            sealed = self.store.db.execute("select ids from control_outbox where kind='result'").fetchone()
            protected = set(json.loads(sealed[0])) if sealed else set()
            for row in self.store.db.execute("select command_id,pending from control_attempts where expires_at<?", (cutoff,)).fetchall():
                if row[0] not in protected:
                    if row[1]:
                        self.store._gap(1, milliseconds(server_time))
                    self.store.db.execute("delete from control_attempts where command_id=?", (row[0],))

    def begin(self, command, server_time):
        immutable = {key: value for key, value in command.items() if key != "remaining_ms"}
        fingerprint = hashlib.sha256(encoded(immutable).encode()).hexdigest()
        with self.store.transaction():
            row = self.store.db.execute("select fingerprint from control_attempts where command_id=?", (command["id"],)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("control_command_conflict")
                return None
            if self.store.db.execute("select count(*) from control_attempts").fetchone()[0] >= MAX_ATTEMPTS:
                raise ValueError("control_attempt_limit")
            attempt = str(uuid4())
            result = {"command_id": command["id"], "attempt_id": attempt, "outcome": "unknown", "reason": "network_unknown"}
            self.store.db.execute("insert into control_attempts values(?,?,?,?,?,1)",
                (command["id"], fingerprint, attempt, command["expires_at"], encoded(result)))
            self.check_budget()
            return attempt

    def finish(self, command, attempt, outcome, reason):
        result = {"command_id": command, "attempt_id": attempt, "outcome": outcome, "reason": reason}
        with self.store.transaction():
            self.store.db.execute("update control_attempts set message=? where command_id=? and attempt_id=?",
                                  (encoded(result), command, attempt))
            self.check_budget()

    def report(self, report):
        with self.store.transaction():
            prior = self.store.db.execute("select message from control_reports where actuator_id=?", (report["actuator_id"],)).fetchone()
            if prior:
                prior = json.loads(prior[0])
                if report["kind"] == "snapshot" and prior["kind"] == "event" and (report["source_time"] or "") <= (prior["source_time"] or ""):
                    return
            previous = self.store.get("control_last_report_id")
            report = {**report, "id": measurement_id(milliseconds(report["reported_at"]), previous)}
            self.store.set("control_last_report_id", report["id"])
            self.store.db.execute("insert into control_reports values(?,?) on conflict(actuator_id) do update set message=excluded.message",
                                  (report["actuator_id"], encoded(report)))
            if self.store.db.execute("select count(*) from control_reports").fetchone()[0] > 16:
                raise ValueError("control_report_limit")
            self.check_budget()

    def next(self):
        with self.store.transaction():
            queued = self.store.db.execute("select kind,message,ids from control_outbox order by kind").fetchone()
            if queued:
                return dict(queued)
            last = self.store.get("control_last_kind")
            reports = self.store.db.execute("select actuator_id,message from control_reports order by actuator_id limit 16").fetchall()
            results = self.store.db.execute("select command_id,message from control_attempts where pending=1 order by rowid limit 16").fetchall()
            if reports and (not results or last != "state"):
                kind, key, rows = "state", "reports", reports
            elif results:
                kind, key, rows = "result", "results", results
            else:
                return None
            ids = [row[0] for row in rows]
            message = encoded({"schema_version": 1, "action": "control_" + kind, key: [json.loads(row[1]) for row in rows]})
            if kind == "state":
                self.store.db.executemany("delete from control_reports where actuator_id=?", [(value,) for value in ids])
            self.store.db.execute("insert into control_outbox values(?,?,?)", (kind, message, encoded(ids)))
            self.store.set("control_last_kind", kind)
            self.check_budget(False)
            return {"kind": kind, "message": message, "ids": encoded(ids)}

    def accepted(self, row):
        with self.store.transaction():
            if row["kind"] == "result":
                self.store.db.executemany("update control_attempts set pending=0 where command_id=?", [(value,) for value in json.loads(row["ids"])])
            self.store.db.execute("delete from control_outbox where kind=? and message=?", (row["kind"], row["message"]))


class ControlRuntime:
    def __init__(self, hass, entry, store, db):
        self.hass, self.entry, self.store, self.db = hass, entry, store, db
        self.storage = ControlStore(store)
        self.entities, self.by_id = {}, {}
        self.offer = {"schema_version": 1, "action": "control_offer", "enabled": False, "sources": [], "actuators": []}
        self.supported = False
        self.listeners = []
        self.queue = asyncio.Queue(maxsize=16)
        self.pending_gap = 0
        self.writer = None
        self.stopped = False

    @property
    def enabled(self):
        return self.option("allow_control", False) is True

    def option(self, key, default):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def wants_nonce(self):
        return self.enabled or await self.db(self.store.get, "control_was_enabled", False)

    async def refresh(self):
        for remove in self.listeners:
            remove()
        self.listeners.clear()
        ids = self.option("control_entities", []) if self.enabled else []
        available = candidates(self.hass) if ids else {}
        previous = self.entities
        self.entities = {key: available[key] for key in ids if key in available}
        if self.enabled:
            from homeassistant.helpers.event import async_track_state_change_event, async_track_state_report_event
            latches = await self.db(self.store.get, "control_protected", {})
            self.offer, latches = control_catalog(self.entry.data["connection_id"], self.entry.data["instance_id"],
                list(self.entities.values()), self.option("control_off_entities", []), latches)
            await self.db(self.storage.configure, self.offer, latches)
            self.by_id = {actuator["id"]: (actuator, entity) for actuator, entity in zip(self.offer["actuators"], sorted(self.entities.values(), key=lambda value: value.registry_id))}
            paths = [entity.entity_id for entity in self.entities.values()]
            self.listeners = [async_track_state_change_event(self.hass, paths, self.state_changed),
                              async_track_state_report_event(self.hass, paths, self.state_changed)]
            if self.writer is None:
                self.writer = self.entry.async_create_background_task(self.hass, self.write_reports(), "nausto-control-state")
            for entity in self.entities.values():
                if previous.get(entity.registry_id) != entity:
                    self.capture(entity, self.hass.states.get(entity.entity_id), None)
        else:
            self.by_id = {}
            self.offer = {"schema_version": 1, "action": "control_offer", "enabled": False, "sources": [], "actuators": []}
            await self.db(self.storage.disable)

    def capture(self, entity, state, event):
        found = next((item for item in self.by_id.values() if item[1].registry_id == entity.registry_id), None)
        if found is None:
            return
        actuator = found[0]
        quality = "unavailable" if state is None or state.state == "unavailable" else "unknown" if state.state not in ("on", "off") or state.attributes.get("assumed_state") is True or state.attributes.get("restored") is True else "good"
        on, level = None, None
        if quality == "good":
            on = state.state == "on"
            brightness = state.attributes.get("brightness")
            if actuator["kind"] == "dimmer" and on and type(brightness) is int and 1 <= brightness <= 255:
                level = brightness / 255
        at = instant(event.time_fired) if event is not None else instant(state.last_reported) if state is not None else None
        if at is None:
            return
        report = {"actuator_id": actuator["id"], "source_id": actuator["source_id"], "on": on, "level": level,
                  "quality": quality, "reported_at": at, "source_time": instant(state.last_reported) if state is not None else None,
                  "kind": "event" if event is not None else "snapshot"}
        try:
            self.queue.put_nowait(report)
        except asyncio.QueueFull:
            self.pending_gap += 1

    @callback
    def state_changed(self, event):
        if self.stopped or not self.enabled:
            return
        from homeassistant.helpers import entity_registry
        registered = entity_registry.async_get(self.hass).async_get(event.data["entity_id"])
        if registered is None or registered.id not in self.entities:
            return
        entity = self.entities[registered.id]
        if entity.entity_id != registered.entity_id:
            return
        self.capture(entity, event.data.get("new_state") or self.hass.states.get(entity.entity_id), event)

    async def write_reports(self):
        while True:
            report = await self.queue.get()
            try:
                await self.db(self.storage.report, report)
                if self.pending_gap:
                    count, self.pending_gap = self.pending_gap, 0
                    await self.db(self.store.note_gap, count, milliseconds(report["reported_at"]))
            except Exception:
                self.pending_gap += 1
            finally:
                self.queue.task_done()

    async def next(self, has_scalar):
        if not self.supported or await self.db(self.store.get, "control_delivery_rejected", False):
            return None
        message = encoded(self.offer)
        if await self.db(self.store.get, "control_offered") != message:
            return {"kind": "offer", "message": message}
        if has_scalar and await self.db(self.store.get, "control_last_was_control", False):
            return None
        try:
            return await self.db(self.storage.next)
        except ValueError:
            self.pending_gap += 1
            return None

    async def accepted(self, row):
        if row["kind"] == "offer":
            await self.db(self.storage.mark_offered, row["message"])
            if not self.enabled:
                await self.db(self.store.set, "control_was_enabled", False)
        else:
            await self.db(self.storage.accepted, row)
        await self.db(self.store.set, "control_last_was_control", True)

    async def receipt(self, data, nonce, elapsed_ms, generation=None):
        received = time.monotonic()
        commands = read_commands(data, nonce, elapsed_ms)
        self.supported = commands is not None
        if commands is None:
            return
        await self.db(self.storage.verified_time, data["control_server_time"])
        if await self.db(self.store.get, "control_delivery_rejected", False):
            self.supported = False
            return
        expected_generation = generation if generation is not None else await self.db(self.store.get, "generation", 0)
        for command in commands:
            deadline = received + command["remaining_ms"] / 1000
            try:
                attempt = await self.db(self.storage.begin, command, data["control_server_time"])
            except Exception:
                # Ingen varig markør betyr ingen fysisk handling.
                self.pending_gap += 1
                continue
            if attempt is None:
                continue
            current_generation = await self.db(self.store.get, "generation", 0)
            reason = None
            found = self.by_id.get(command["actuator_id"])
            current = candidates(self.hass) if self.enabled else {}
            if self.stopped or not self.enabled or current_generation != expected_generation:
                reason = "local_disabled"
            elif found is None or found[1].registry_id not in current or current[found[1].registry_id] != found[1]:
                reason = "unsupported"
            elif found[1].registry_id not in self.option("control_entities", []):
                reason = "local_disabled"
            elif any(command[key] != found[0][key] for key in ("source_id", "external_id", "kind", "level_steps")):
                reason = "unsupported"
            elif (command["critical"] or found[0]["protected"] or found[1].registry_id not in self.option("control_off_entities", [])) and not command["target"]["on"]:
                reason = "provider_rejected"
            elif time.monotonic() >= deadline:
                reason = "expired"
            if reason:
                await self.db(self.storage.finish, command["id"], attempt, "rejected", reason)
                continue
            # Samtykke og monoton levetid kontrolleres igjen umiddelbart før ett konkret HA-kall.
            if self.stopped or not self.enabled or time.monotonic() >= deadline:
                await self.db(self.storage.finish, command["id"], attempt, "rejected", "local_disabled" if self.stopped or not self.enabled else "expired")
                continue
            entity = found[1]
            domain = entity.entity_id.partition(".")[0]
            values = {"entity_id": entity.entity_id}
            target = command["target"]
            if target["on"] and target["level"] is not None:
                values["brightness"] = round(target["level"] * 255)
            try:
                await self.hass.services.async_call(domain, "turn_on" if target["on"] else "turn_off", values, blocking=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.db(self.storage.finish, command["id"], attempt, "unknown", "network_unknown")
            else:
                await self.db(self.storage.finish, command["id"], attempt, "acknowledged", None)
            # Én ny kontrollhandling får kildebelegget ut før vanlig fairness gjenopptas.
            await self.db(self.store.set, "control_last_was_control", False)

    async def stop(self):
        self.stopped = True
        for remove in self.listeners:
            remove()
        self.listeners.clear()
        if self.writer:
            await self.queue.join()
            self.writer.cancel()
            await asyncio.gather(self.writer, return_exceptions=True)
        if self.pending_gap:
            await self.db(self.store.note_gap, self.pending_gap, int(time.time() * 1000))
