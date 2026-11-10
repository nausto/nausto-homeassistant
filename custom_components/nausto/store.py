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
        """)

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
        self.db.execute("insert into batches(id,message,created,bytes,count) values(?,?,?,?,?)", (
            batch["batch_id"], message, milliseconds(batch["created_at"]), len(message.encode()), len(batch["observations"])))
        state["batch"] = None
        state["urgent"] = False
        self._prune(now)

    def capture(self, channel: dict, reading: dict, now: int) -> bool:
        with self.transaction():
            if (channel["id"] not in self.get("selected", []) or
                    not any(value["id"] == channel["id"] for value in self.get("catalog", {}).get("channels", []))):
                return False
            state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
            self._seal(state, now)
            source_at = milliseconds(reading["confirmed_at"] or reading["observed_at"])
            if source_at < now - RETENTION_SECONDS * 1000:
                self.set("captures", state)
                return False
            fingerprint = hashlib.sha256(encoded(reading).encode()).hexdigest()
            previous = state["cursors"].get(channel["id"])
            transition = previous is not None and (
                previous.get("quality") != reading["quality"] or
                (channel["quantity"] in ("state", "alarm") and previous.get("value") != reading["value"]))
            quiet = previous is not None and (
                abs(reading["value"] - previous["value"]) < DEADBANDS.get(channel["quantity"], 0)
                if type(reading["value"]) in (int, float) and type(previous["value"]) in (int, float)
                else previous["value"] == reading["value"])
            if previous and (previous["fingerprint"] == fingerprint or (not transition and (
                    now - previous["at"] < 60_000 or (quiet and now - previous["at"] < REPORT_INTERVAL)))):
                self.set("captures", state)
                return False
            observation = {**reading, "measurement_id": measurement_id(now, state.get("last_id"))}
            batch = state.get("batch") or {"schema_version": 1, "action": "batch", "batch_id": str(uuid4()), "created_at": stamp(now), "observations": []}
            candidate = {**batch, "observations": [*batch["observations"], observation]}
            if len(candidate["observations"]) > MAX_OBSERVATIONS or len(encoded(candidate).encode()) > MAX_BYTES:
                self._seal(state, now, True)
                candidate = {"schema_version": 1, "action": "batch", "batch_id": str(uuid4()), "created_at": stamp(now), "observations": [observation]}
            if len(encoded(candidate).encode()) > MAX_BYTES:
                raise ValueError("observation_limit")
            state["batch"], state["last_id"] = candidate, observation["measurement_id"]
            state["urgent"] = bool(state.get("urgent") or transition)
            state["cursors"][channel["id"]] = {"fingerprint": fingerprint, "at": now, "value": reading["value"], "quality": reading["quality"]}
            # Cursormengden følger den avgrensede katalogen, ikke alle historiske HA-entiteter.
            if len(state["cursors"]) > 64:
                old_ids = [key for key in state["cursors"] if key != channel["id"]]
                oldest = min(old_ids, key=lambda key: state["cursors"][key]["at"])
                del state["cursors"][oldest]
            self.set("captures", state)
            self._prune(now)
            return True

    def has_urgent(self) -> bool:
        state = self.get("captures", {})
        return bool(state.get("batch") and state.get("urgent"))

    def seal_urgent(self, now: int):
        with self.transaction():
            state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
            if state.get("urgent"):
                self._seal(state, now, True)
                self.set("captures", state)

    def _reconcile(self, allowed, now: int):
        state = self.get("captures", {"batch": None, "cursors": {}, "last_id": None})
        state["cursors"] = {key: value for key, value in state["cursors"].items() if key in allowed}
        if state.get("batch"):
            before = state["batch"]["observations"]
            kept = [o for o in before if o["channel_id"] in allowed]
            if len(kept) != len(before):
                self._gap(len(before) - len(kept), now)
                state["batch"] = {**state["batch"], "observations": kept} if kept else None
                if not kept:
                    state["urgent"] = False
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
