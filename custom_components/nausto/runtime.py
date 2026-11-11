"""HA-hendelser til varig kø, med uavhengig skriving og avgrenset utgående sending."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import hashlib
import secrets
import time

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers import entity_registry, issue_registry

from .client import Client
from .control import ControlRuntime
from .const import INTERVAL_SECONDS, MAX_ATTEMPTS
from .entities import metadata, snapshots
from .mapping import catalog, discovery, instant, kind, reading
from .store import encoded
from .profiles import connection_id


def clock() -> int:
    return int(time.time() * 1000)


class Runtime:
    def __init__(self, hass, entry, profiles, store, client: Client):
        self.hass, self.entry, self.profiles, self.store, self.client = hass, entry, profiles, store, client
        self.connection_id = entry.data["connection_id"]
        self.instance_id = entry.data["instance_id"]
        self.registry_ids = entry.options.get("entities", entry.data.get("entities", []))
        self.entities, self.channels, self.catalog = {}, {}, {"sources": [], "channels": []}
        self.selected = set()
        self.pending = asyncio.Queue(maxsize=100)
        self.pending_gap = 0
        self.stopped, self.authorized, self.reauth_requested = False, False, False
        self.failure, self.local_failure = None, False
        self.network = asyncio.Lock()
        self.scanning = asyncio.Lock()
        self.processing = asyncio.Lock()
        self.state_unsubscribe = None
        self.listeners = []
        self.writer = self.rescan_task = self.delivery_task = None
        self.delivery_wakeup = None
        self.next_scan = 0
        self.actuation = ControlRuntime(hass, entry, store, self.db)

    async def db(self, function, *args):
        return await self.hass.async_add_executor_job(function, *args)

    async def async_start(self):
        self.generation = await self.db(self.store.get, "generation", 0)
        control = await self.db(self.store.get, "control", {})
        if control.get("failure") != "unauthorized":
            self.selected = set(await self.db(self.store.get, "selected", []))
        await self.refresh()
        self.writer = self.entry.async_create_background_task(self.hass, self.write_events(), "nausto-capture")
        self.listeners = [
            self.hass.bus.async_listen("entity_registry_updated", self.registry_changed),
            self.hass.bus.async_listen("device_registry_updated", self.registry_changed),
            async_track_time_interval(self.hass, self.periodic, timedelta(seconds=INTERVAL_SECONDS)),
        ]
        self.entry.async_on_unload(self.unsubscribe)
        self.poll()
        await self.tick()
        await self.maintain_delivery()
        await self.health()

    async def refresh(self):
        async with self.scanning:
            if self.stopped:
                return
            await self._refresh()

    async def _refresh(self):
        previous = await self.db(self.store.get, "entity_metadata", {})
        values = snapshots(self.hass, self.registry_ids, previous)
        offered = catalog(self.connection_id, self.instance_id, values)
        await self.db(self.store.configure, offered, clock())
        await self.db(self.store.set, "entity_metadata", metadata(values))
        self.catalog = offered
        self.entities = {entity.registry_id: entity for entity in values}
        self.channels = {channel["_registry_id"]: channel for channel in offered["channels"]}
        self.revision = hashlib.sha256(encoded(discovery(offered)).encode()).hexdigest()
        if self.state_unsubscribe:
            self.state_unsubscribe()
        self.state_unsubscribe = async_track_state_change_event(self.hass, [entity.entity_id for entity in values], self.state_changed)
        self.next_scan = clock() + 60_000
        await self.actuation.refresh()
        if len({source["id"] for source in offered["sources"] + self.actuation.offer["sources"]}) > 16:
            self.actuation.by_id.clear()
            raise ValueError("source_limit")

    @callback
    def registry_changed(self, _event):
        if self.stopped or self.rescan_task is not None and not self.rescan_task.done():
            return
        self.rescan_task = self.entry.async_create_background_task(self.hass, self.rescan(), "nausto-registry")

    async def rescan(self):
        try:
            await self.refresh()
            self.poll()
        except Exception:
            self.local_failure = True

    @callback
    def state_changed(self, event):
        if self.stopped:
            return
        registered = entity_registry.async_get(self.hass).async_get(event.data["entity_id"])
        if registered is None or registered.disabled_by is not None:
            return
        entity = self.entities.get(registered.id)
        if entity is None or entity.entity_id != registered.entity_id:
            return
        state = event.data.get("new_state")
        if state is None:
            current = replace(entity, state=None, reported_at=instant(event.time_fired), restored=False)
        else:
            current = replace(entity, state=state.state,
                              unit=state.attributes.get("unit_of_measurement", entity.unit),
                              device_class=state.attributes.get("device_class", entity.device_class),
                              updated_at=instant(state.last_updated), reported_at=instant(state.last_reported),
                              restored=state.attributes.get("restored") is True)
        if kind(current) != kind(entity):
            self.registry_changed(event)
            return
        self.entities[entity.registry_id] = current
        self.enqueue(current)

    def enqueue(self, entity):
        channel = self.channels.get(entity.registry_id)
        if self.stopped or channel is None or channel["id"] not in self.selected:
            return
        value = reading(entity, channel)
        if value is None:
            return
        try:
            self.pending.put_nowait((channel, value, clock()))
        except asyncio.QueueFull:
            self.pending_gap += 1

    def poll(self):
        # Les rapporttiden fra HA. Periodisk kjøring setter aldri et nytt kildetidspunkt.
        for entity in snapshots(self.hass, self.registry_ids, metadata(list(self.entities.values()))):
            self.enqueue(entity)

    async def write_events(self):
        while True:
            item = await self.pending.get()
            try:
                captured = await self.db(self.store.capture, *item)
                if captured and await self.db(self.store.has_urgent):
                    self.request_delivery()
                if self.pending_gap:
                    count, self.pending_gap = self.pending_gap, 0
                    await self.db(self.store.note_gap, count, clock())
            except Exception:
                self.local_failure = True
                self.pending_gap += 1
            finally:
                self.pending.task_done()

    @callback
    def request_delivery(self):
        # Skriving venter aldri på HTTP. Raske overganger deler ett varig vindu til senderen
        # får neste lovlige plass; tick beholder fartsgrense, autorisasjon og serverens backoff.
        if self.stopped or self.delivery_wakeup is not None or self.delivery_task and not self.delivery_task.done():
            return
        self.delivery_task = self.entry.async_create_background_task(
            self.hass, self.deliver_changes(), "nausto-delivery")

    async def maintain_delivery(self):
        if self.stopped or not self.authorized:
            return
        urgent = bool(self.selected) and await self.db(self.store.has_urgent)
        if urgent or await self.actuation.wants_nonce():
            self.request_delivery()
        elif self.delivery_wakeup is not None:
            self.delivery_wakeup.cancel()
            self.delivery_wakeup = None

    async def deliver_changes(self):
        try:
            await self.tick()
            if self.stopped or not self.authorized:
                return
            if not await self.actuation.wants_nonce() and (not self.selected or not await self.db(self.store.has_urgent)):
                return
            control = await self.db(self.store.get, "control", {"attempts": 0, "retry_at": 0, "failure": None})
            if control["failure"] in ("unauthorized", "rejected", "attempts_exhausted"):
                return
            last = await self.db(self.store.get, "last_request", 0)
            due = max(last + 30_000, control["retry_at"])
            # Et hendelsesutløst kall følger ikke nødvendigvis periodikkens 30-sekundersfase.
            # En egen, koalescert vekking treffer neste lovlige luke uten å vente på neste poll.
            if not self.stopped:
                self.delivery_wakeup = self.hass.loop.call_later(
                    max(.1, (due - clock()) / 1000), self.wake_delivery)
        except Exception:
            self.local_failure = True

    @callback
    def wake_delivery(self):
        self.delivery_wakeup = None
        self.request_delivery()

    async def periodic(self, _now):
        if self.stopped or self.processing.locked():
            return
        async with self.processing:
            if not self.stopped:
                await self._periodic()

    async def _periodic(self):
        try:
            if clock() >= self.next_scan:
                await self.refresh()
                await self.db(self.profiles.prune, clock())
            self.poll()
            await self.tick()
            await self.maintain_delivery()
            await self.health()
        except Exception:
            self.local_failure = True

    async def tick(self):
        if self.stopped or self.network.locked():
            return
        async with self.network:
            now = clock()
            await self.db(self.store.seal, now)
            key = await self.db(self.store.get, "key")
            generation = await self.db(self.store.get, "generation", 0)
            control = await self.db(self.store.get, "control", {"attempts": 0, "retry_at": 0, "failure": None})
            if not key or generation != self.generation or control["failure"] == "unauthorized":
                self.selected.clear()
                if not self.reauth_requested:
                    self.entry.async_start_reauth(self.hass)
                    self.reauth_requested = True
                return
            if control["failure"] in ("rejected", "attempts_exhausted"):
                return
            last = await self.db(self.store.get, "last_request", 0)
            if now - last < 30_000 or now < control["retry_at"] or control["attempts"] >= MAX_ATTEMPTS:
                return
            offered = await self.db(self.store.get, "offered")
            last_control = await self.db(self.store.get, "last_control", 0)
            dirty = bool(self.catalog["channels"]) and offered != self.revision
            row, actuation_row = None, None
            if dirty:
                message = encoded(discovery(self.catalog))
            elif not self.authorized or now - last_control >= 120_000:
                message = encoded({"schema_version": 1, "action": "heartbeat"})
            else:
                await self.db(self.store.seal_urgent, now)
                row = await self.db(self.store.next, now)
                actuation_row = await self.actuation.next(row is not None)
                if actuation_row is not None:
                    row = None
                    message = actuation_row["message"]
                elif row is None:
                    # Også en rolig kilde må hente kortlivede kommandoer ved neste
                    # lovlige luke. Kømeldinger bærer samme nonce og beholder forrang.
                    if not await self.actuation.wants_nonce():
                        return
                    message = encoded({"schema_version": 1, "action": "heartbeat"})
                else:
                    message = row["message"]
            await self.db(self.store.set, "last_request", now)
            if self.stopped:
                return
            nonce = secrets.token_hex(16) if await self.actuation.wants_nonce() else None
            started = time.monotonic()
            result = await self.client.send(key, message, nonce) if nonce else await self.client.send(key, message)
            if self.stopped or await self.db(self.store.get, "generation", 0) != self.generation:
                return
            elapsed = (time.monotonic() - started) * 1000
            if result.ok and nonce is not None:
                await self.actuation.receipt(result.data, nonce, elapsed, self.generation)
            if result.ok and actuation_row is not None:
                await self.actuation.accepted(actuation_row)
                await self.db(self.store.set, "last_accepted", now)
                await self.db(self.store.set, "control", {"attempts": 0, "retry_at": 0, "failure": None})
                self.failure = None
                await self.maintain_delivery()
                return
            if result.ok and row is not None:
                await self.db(self.store.accepted, row["id"])
                await self.db(self.store.set, "last_accepted", now)
                await self.db(self.store.set, "control", {"attempts": 0, "retry_at": 0, "failure": None})
                self.failure = None
                await self.db(self.store.set, "control_last_was_control", False)
                return
            if actuation_row is not None and result.status in (400, 403, 409, 413):
                # Et additivt kontrollavslag får ikke stanse den etablerte målekanalen.
                await self.db(self.store.set, "control_delivery_rejected", True)
                await self.db(self.store.set, "control", {"attempts": 0, "retry_at": now + 30_000, "failure": None})
                self.actuation.supported, self.local_failure = False, True
                return
            ids = result.data.get("selected_channel_ids") if result.data else None
            try:
                valid_ids = isinstance(ids, list) and len(ids) <= 64 and all(connection_id(value) == value for value in ids)
            except ValueError:
                valid_ids = False
            if result.ok and row is None and valid_ids:
                offered_ids = {channel["id"] for channel in self.catalog["channels"]}
                self.selected = set(ids) & offered_ids
                await self.db(self.store.select, sorted(self.selected), now)
                await self.db(self.store.set, "last_control", now)
                if dirty:
                    await self.db(self.store.set, "offered", self.revision)
                await self.db(self.store.set, "control", {"attempts": 0, "retry_at": 0, "failure": None})
                self.authorized, self.failure = True, None
                self.poll()
                return
            if row is not None:
                await self.db(self.store.failed, row["id"], result.status, result.retry, now)
                if result.status == 403:
                    # Eieren kan ha fjernet en kanal siden forrige livstegn. Batchen
                    # beholdes avvist til et nytt kontrollsvar avstemmer delingsvalget.
                    self.authorized, self.failure = False, "connection"
                    await self.db(self.store.set, "control", {"attempts": 0, "retry_at": now + 30_000, "failure": "connection"})
                    return
            attempts = control["attempts"] + 1
            failure = "unauthorized" if result.status == 401 else "rejected" if result.status in (400, 403, 409, 413) else "attempts_exhausted" if attempts >= MAX_ATTEMPTS else "connection"
            delay = min(3600, max(result.retry, 2 ** min(attempts, 11)))
            await self.db(self.store.set, "control", {"attempts": attempts, "retry_at": now + delay * 1000, "failure": failure})
            self.failure = failure
            if result.status == 401:
                self.authorized = False
                self.selected.clear()
                if not self.reauth_requested:
                    self.entry.async_start_reauth(self.hass)
                    self.reauth_requested = True

    async def health(self):
        status = await self.db(self.store.status)
        discarded = await self.db(self.profiles.index.get, "discarded", 0)
        identifier = self.entry.entry_id
        if self.local_failure or self.actuation.pending_gap or discarded or status["rejected_batches"] or status["gap_observations"] or status["control"]["failure"] in ("rejected", "attempts_exhausted") or await self.db(self.store.get, "control_delivery_rejected", False):
            issue_registry.async_create_issue(self.hass, "nausto", identifier, is_fixable=False,
                severity=issue_registry.IssueSeverity.WARNING, translation_key="delivery")
        else:
            issue_registry.async_delete_issue(self.hass, "nausto", identifier)

    @callback
    def unsubscribe(self):
        if self.state_unsubscribe:
            self.state_unsubscribe()
            self.state_unsubscribe = None
        for unsubscribe in self.listeners:
            unsubscribe()
        self.listeners.clear()

    async def async_stop(self):
        self.stopped = True
        self.actuation.stopped = True
        if self.delivery_wakeup is not None:
            self.delivery_wakeup.cancel()
            self.delivery_wakeup = None
        self.unsubscribe()
        if self.rescan_task and not self.rescan_task.done():
            self.rescan_task.cancel()
            await asyncio.gather(self.rescan_task, return_exceptions=True)
        # Et tidsstyrt registeroppslag kan allerede være i gang når lytteren fjernes.
        # Vent før siste unsubscribe og før noen av databasene lukkes.
        async with self.processing:
            pass
        async with self.scanning:
            self.unsubscribe()
        if self.writer:
            try:
                await asyncio.wait_for(self.pending.join(), timeout=5)
            except asyncio.TimeoutError:
                await self.db(self.store.note_gap, self.pending.qsize(), clock())
            self.writer.cancel()
            await asyncio.gather(self.writer, return_exceptions=True)
        if self.delivery_task:
            await asyncio.gather(self.delivery_task, return_exceptions=True)
        # En utestående kvittering må være ferdig før SQLite-forbindelsen lukkes.
        async with self.network:
            await self.actuation.stop()
            if self.pending_gap:
                await self.db(self.store.note_gap, self.pending_gap, clock())
                self.pending_gap = 0
            await self.db(self.store.seal, clock(), True)
            await self.db(self.store.close)
            await self.db(self.profiles.close)
