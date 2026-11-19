"""Varig, privat og avgrenset produsentkø. Nettfeil endrer aldri et forseglet opptak."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from threading import RLock
from uuid import UUID, uuid4

from .const import MAX_ATTEMPTS, MAX_BYTES, MAX_OBSERVATIONS, QUEUE_BYTES, QUEUE_OBSERVATIONS, RETENTION_SECONDS


# Kontraktens SI-enheter. To minutter er kortere enn minste ferskhetsvalg på fem minutter.
DEADBANDS = {"temperature": .1, "voltage": .05, "current": .1, "power": 1,
             "charge": 36, "ratio": .001, "speed": .05, "angle": .0017453292519943296,
             "rotation": 1 / 60, "duration": 1}
REPORT_INTERVAL = 120_000


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def stamp(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def milliseconds(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def measurement_id(now: int, previous: str | None) -> str:
    """Stigende UUIDv7; ID-klokken erstatter aldri en kildeklokke."""
    timestamp, random = now, secrets.randbits(74)
    if previous:
        old = UUID(previous).int
        if old >> 80 >= timestamp:
            timestamp = old >> 80
            random = (((old >> 64) & 0xFFF) << 62 | (old & ((1 << 62) - 1))) + 1
            if random >= 1 << 74:
                timestamp, random = timestamp + 1, 0
    bits = timestamp << 80 | 7 << 76 | (random >> 62) << 64 | 2 << 62 | (random & ((1 << 62) - 1))
    return str(UUID(int=bits))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if path.is_symlink():
            raise ValueError("invalid_store")
        self.lock = RLock()
        # Livevisningen (del 2 av #2295): siste liveverdi per kanal med avtrykket, og rekken for ID-er
        # bare strømmen får. Begge står bare i minnet, så batchenes varige rekke aldri flyttes av dem.
        self.known, self.live_id = {}, None
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=full")
        self.db.execute("pragma busy_timeout=5000")
        self.db.executescript("""
          create table if not exists metadata(key text primary key,value text not null);
          create table if not exists batches(
            id text primary key,message text not null,created integer not null,
            bytes integer not null,count integer not null,
            attempts integer not null default 0,retry_at integer not null default 0,rejected text);
          create table if not exists gaps(id integer primary key check(id=1),observations integer not null,first_at integer,last_at integer);
          insert or ignore into gaps(id,observations) values(1,0);
          create table if not exists live(channel text primary key,observation text not null);
        """)
        with self.transaction():
            # 0.5.0 merker et forseglet vindu med en alarmovergang (ADR 320). Kolonnen kan stå tom, så
            # en eldre versjon leser og skriver køen som før.
            if "alarm_at" not in {row[1] for row in self.db.execute("pragma table_info(batches)")}:
                self.db.execute("alter table batches add column alarm_at integer")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("begin immediate")
            try:
                yield
                self.db.execute("commit")
            except BaseException:
                self.db.execute("rollback")
                raise

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("select value from metadata where key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        with self.lock:
            self.db.execute("insert into metadata values(?,?) on conflict(key) do update set value=excluded.value", (key, encoded(value)))

    def _gap(self, count: int, at: int):
        self.db.execute("update gaps set observations=observations+?,first_at=coalesce(first_at,?),last_at=? where id=1", (count, at, at))

    def _prune(self, now: int):
        expired = self.db.execute("select id,count from batches where created<?", (now - RETENTION_SECONDS * 1000,)).fetchall()
        for row in expired:
            self._gap(row["count"], now)
            self.db.execute("delete from batches where id=?", (row["id"],))
        while True:
            total = self.db.execute("select coalesce(sum(count),0),coalesce(sum(bytes),0) from batches").fetchone()
            if total[0] <= QUEUE_OBSERVATIONS and total[1] <= QUEUE_BYTES:
                break
            row = self.db.execute("select id,count from batches order by created,rowid limit 1").fetchone()
            self._gap(row["count"], now)
            self.db.execute("delete from batches where id=?", (row["id"],))

    def _seal(self, state: dict, now: int, force=False):
        batch = state.get("batch")
        if batch is None or (not force and now - milliseconds(batch["created_at"]) < 60_000):
            return
        message = encoded(batch)
        self.db.execute("insert into batches(id,message,created,bytes,count,alarm_at) values(?,?,?,?,?,?)", (
            batch["batch_id"], message, milliseconds(batch["created_at"]), len(message.encode()), len(batch["observations"]),
            state.pop("alarm_at", None)))
        state["batch"] = None
        state["urgent"] = False
        self._prune(now)

    def capture(self, channel: dict, reading: dict, now: int) -> dict | bool:
        """Observasjonen som gikk til batchen, eller False."""
        return self.record(channel, reading, now)[0]

    def record(self, channel: dict, reading: dict | None, now: int) -> tuple[dict | bool, dict | None]:
        """Opptaket og livevisningens del: batchens observasjon eller False, og en ny liveverdi eller None.

        Livevisningen får hver ny, datert tilstand, også når batchen venter med den, med en ID fra
        rekken bare strømmen har. Kommer samme observasjon senere i en batch, beholder den ID-en, så
        strømmen og batchen er enige. En gjentatt tilstand og en verdi eldre enn sju dager går ikke
        live, og uten avlesning (None) er HAs verdi udatert: kanalen står utenfor øyeblikksbildet til
        neste daterte verdi."""
        if reading is None:
            with self.lock:
                self.known.pop(channel["id"], None)
            return False, None
        with self.transaction():
            if (channel["id"] not in self.get("selected", []) or
                    not any(value["id"] == channel["id"] for value in self.get("catalog", {}).get("channels", []))):
                return False, None
            state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
            self._seal(state, now)
            source_at = milliseconds(reading["confirmed_at"] or reading["observed_at"])
            if source_at < now - RETENTION_SECONDS * 1000:
                self.set("captures", state)
                return False, None
            fingerprint = hashlib.sha256(encoded(reading).encode()).hexdigest()
            previous = state["cursors"].get(channel["id"])
            transition = previous is not None and (
                previous.get("quality") != reading["quality"] or
                (channel["quantity"] in ("state", "alarm") and previous.get("value") != reading["value"]))
            # En alarmovergang er en ny verdi som ikke er null på en av/på- eller alarmkanal, fra en kjent
            # tidligere avlesning. Den kan gå på alarmsporet; endret kvalitet og tall følger urgent.
            alarm = (previous is not None and channel["quantity"] in ("state", "alarm") and
                     reading["value"] is not None and previous.get("value") != reading["value"])
            quiet = previous is not None and (
                abs(reading["value"] - previous["value"]) < DEADBANDS.get(channel["quantity"], 0)
                if type(reading["value"]) in (int, float) and type(previous["value"]) in (int, float)
                else previous["value"] == reading["value"])
            repeated = previous is not None and previous["fingerprint"] == fingerprint
            # Samme observasjon som siste liveverdi, som en tilstand lest på nytt, beholder ID-en den fikk.
            known = self.known.get(channel["id"])
            same = known["observation"] if known is not None and known["fingerprint"] == fingerprint else None
            if previous and (repeated or (not transition and (
                    now - previous["at"] < 60_000 or (quiet and now - previous["at"] < REPORT_INTERVAL)))):
                self.set("captures", state)
                if repeated or same is not None:
                    return False, None
                # Batchen venter, men en ny, datert verdi er likevel siste kjente for livevisningen.
                self.live_id = measurement_id(now, self.live_id)
                live = {**reading, "measurement_id": self.live_id}
                self.known[channel["id"]] = {"fingerprint": fingerprint, "observation": live}
                return False, live
            identifier = same["measurement_id"] if same is not None else measurement_id(now, state.get("last_id"))
            observation = {**reading, "measurement_id": identifier}
            batch = state.get("batch") or {"schema_version": 1, "action": "batch", "batch_id": str(uuid4()), "created_at": stamp(now), "observations": []}
            candidate = {**batch, "observations": [*batch["observations"], observation]}
            if len(candidate["observations"]) > MAX_OBSERVATIONS or len(encoded(candidate).encode()) > MAX_BYTES:
                self._seal(state, now, True)
                candidate = {"schema_version": 1, "action": "batch", "batch_id": str(uuid4()), "created_at": stamp(now), "observations": [observation]}
            if len(encoded(candidate).encode()) > MAX_BYTES:
                raise ValueError("observation_limit")
            state["batch"] = candidate
            if same is None:
                # En ID fra liverekken flytter ikke batchenes rekke.
                state["last_id"] = identifier
            state["urgent"] = bool(state.get("urgent") or transition)
            if alarm:
                # Kildetiden, som serverens ferskhet måles mot; aldri senere enn opptaket.
                state["alarm_at"] = max(state.get("alarm_at") or 0, min(now, source_at))
            state["cursors"][channel["id"]] = {"fingerprint": fingerprint, "at": now, "value": reading["value"], "quality": reading["quality"]}
            # Siste batchede opptak per kanal til øyeblikksbildet etter omstart; i samme transaksjon som
            # opptaket, uten en egen commit. En verdi bare strømmen har fått, står aldri her.
            self.db.execute("insert into live values(?,?) on conflict(channel) do update set observation=excluded.observation",
                            (channel["id"], encoded(observation)))
            # Cursormengden følger den avgrensede katalogen, ikke alle historiske HA-entiteter.
            if len(state["cursors"]) > 64:
                old_ids = [key for key in state["cursors"] if key != channel["id"]]
                oldest = min(old_ids, key=lambda key: state["cursors"][key]["at"])
                del state["cursors"][oldest]
            self.set("captures", state)
            self._prune(now)
            self.known[channel["id"]] = {"fingerprint": fingerprint, "observation": observation}
            # Er den allerede sendt live med samme ID, blir den ingen ny liveverdi.
            return observation, None if same is not None else observation

    def has_urgent(self) -> bool:
        state = self.get("captures", {})
        return bool(state.get("batch") and state.get("urgent"))

    def latest(self) -> list[dict]:
        """Siste opptak per kanal for livevisningen, med sin egen ID og sine tider.

        Bare der det fortsatt er kanalens siste opptak: en eldre versjon skriver ikke tabellen, så
        avtrykket i kanalens markør avgjør, og en kopi som ikke stemmer, sendes aldri."""
        with self.lock:
            cursors = self.get("captures", {}).get("cursors", {})
            result = []
            for channel, text in self.db.execute("select channel,observation from live order by rowid").fetchall():
                observation = json.loads(text)
                reading = {key: value for key, value in observation.items() if key != "measurement_id"}
                cursor = cursors.get(channel) or {}
                if cursor.get("fingerprint") == hashlib.sha256(encoded(reading).encode()).hexdigest():
                    result.append(observation)
            return result

    def last_alarm(self, since: int) -> int | None:
        """Kildetiden for den nyeste alarmovergangen fra since i et åpent eller ikke avvist vindu."""
        with self.lock:
            state = self.get("captures", {})
            value = self.db.execute("select max(alarm_at) from batches where rejected is null and alarm_at>=?", (since,)).fetchone()[0]
            if state.get("batch") and state.get("alarm_at") is not None and state["alarm_at"] >= since:
                value = max(value or 0, state["alarm_at"])
            return value

    def seal_alarm(self, now: int, since: int):
        """Forsegler det åpne vinduet når det har en alarmovergang fra since, så sporet sender akkurat det."""
        with self.transaction():
            state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
            if state.get("batch") and state.get("alarm_at") is not None and state["alarm_at"] >= since:
                self._seal(state, now, True)
                self.set("captures", state)

    def next_alarm(self, since: int, skip=()):
        """Det eldste forseglede vinduet med en alarmovergang fra etter since som sporet ikke har avvist.

        Sporet tar ikke hensyn til batchens egen ventetid; den hører til den vanlige veien."""
        with self.lock:
            rows = self.db.execute("select * from batches where rejected is null and alarm_at>=? order by created,rowid limit ?",
                                   (since, len(skip) + 1)).fetchall()
            return next((dict(row) for row in rows if row["id"] not in skip), None)

    def seal_urgent(self, now: int):
        with self.transaction():
            state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
            if state.get("urgent"):
                self._seal(state, now, True)
                self.set("captures", state)

    def _reconcile(self, allowed, now: int):
        state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
        state["cursors"] = {key: value for key, value in state["cursors"].items() if key in allowed}
        # Som markøren: en kanal som velges inn igjen, får nye ID-er, så ingen ID står i to batcher.
        self.known = {key: value for key, value in self.known.items() if key in allowed}
        for (channel,) in self.db.execute("select channel from live").fetchall():
            if channel not in allowed:
                self.db.execute("delete from live where channel=?", (channel,))
        if state.get("batch"):
            before = state["batch"]["observations"]
            kept = [o for o in before if o["channel_id"] in allowed]
            if len(kept) != len(before):
                self._gap(len(before) - len(kept), now)
                state["batch"] = {**state["batch"], "observations": kept} if kept else None
                if not kept:
                    state["urgent"] = False
                    state.pop("alarm_at", None)
        for row in self.db.execute("select id,message,count from batches").fetchall():
            if any(o["channel_id"] not in allowed for o in json.loads(row["message"])["observations"]):
                # Forseglet innhold omskrives aldri, heller ikke etter et tapt svar.
                self._gap(row["count"], now)
                self.db.execute("delete from batches where id=?", (row["id"],))
        self.set("captures", state)
        self._prune(now)

    def configure(self, catalog: dict, now: int):
        """Begge delingsvalg gjelder også gamle opptak og utestående executor-jobber."""
        allowed = {channel["id"] for channel in catalog["channels"]}
        with self.transaction():
            self.set("catalog", catalog)
            self._reconcile(allowed & set(self.get("selected", [])), now)

    def select(self, channel_ids: list[str], now: int):
        with self.transaction():
            offered = {channel["id"] for channel in self.get("catalog", {}).get("channels", [])}
            allowed = set(channel_ids) & offered
            self.set("selected", sorted(allowed))
            self._reconcile(allowed, now)

    def seal(self, now: int, force=False):
        with self.transaction():
            state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
            self._seal(state, now, force)
            self.set("captures", state)
            self._prune(now)

    def next(self, now: int):
        with self.lock:
            row = self.db.execute("select * from batches where rejected is null and retry_at<=? order by created,rowid limit 1", (now,)).fetchone()
            return dict(row) if row else None

    def accepted(self, batch_id: str):
        with self.lock:
            self.db.execute("delete from batches where id=?", (batch_id,))

    def failed(self, batch_id: str, status: int, retry_seconds: int, now: int):
        with self.transaction():
            row = self.db.execute("select attempts from batches where id=?", (batch_id,)).fetchone()
            if row is None:
                return
            attempts = row[0] + 1
            reason = ("rejected" if status in (400, 403, 409, 413) else "attempts_exhausted" if attempts >= MAX_ATTEMPTS else None)
            delay = min(3600, max(retry_seconds, 2 ** min(attempts, 11)))
            self.db.execute("update batches set attempts=?,retry_at=?,rejected=? where id=?", (attempts, now + delay * 1000, reason, batch_id))

    def retry(self):
        with self.transaction():
            self.db.execute("update batches set attempts=0,retry_at=0,rejected=null")
            self.set("control", {"attempts": 0, "retry_at": 0, "failure": None})
            self.set("control_delivery_rejected", False)

    def note_gap(self, count: int, now: int):
        if count > 0:
            with self.transaction():
                self._gap(count, now)

    def status(self) -> dict:
        """Bare tellere; aldri nøkler, råmålinger eller HTTP-feilmeldinger."""
        with self.lock:
            row = self.db.execute("select count(*),coalesce(sum(count),0),coalesce(sum(rejected is not null),0),coalesce(sum(bytes),0) from batches").fetchone()
            state = self.get("captures", {})
            gap = self.db.execute("select observations,first_at,last_at from gaps where id=1").fetchone()
            return {"batches": row[0], "queued_observations": row[1], "rejected_batches": row[2], "queue_bytes": row[3],
                    "open_observations": len(state.get("batch", {}).get("observations", [])) if state.get("batch") else 0,
                    "gap_observations": gap[0], "gap_first_at": gap[1], "gap_last_at": gap[2],
                    "control": self.get("control", {"attempts": 0, "retry_at": 0, "failure": None})}

    def close(self):
        with self.lock:
            self.db.close()
