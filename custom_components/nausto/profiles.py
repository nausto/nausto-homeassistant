"""Paringsnøkkel før nettverk og høyst fem private profiler, også gjennom avbrutt oppsett."""

import hashlib
from pathlib import Path
import re
import secrets
from threading import RLock
from uuid import UUID

from .store import Store

# Flere HA-oppsettsflyter kan svare samtidig. Én aktivering per privat profilmengde.
_ACTIVATION = RLock()


def connection_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("wrong_connection")
    try:
        result = str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("wrong_connection") from None
    if result != value:
        raise ValueError("wrong_connection")
    return result


class Profiles:
    def __init__(self, directory: Path, instance_id: str):
        self.directory, self.instance_id = directory, instance_id
        self.index = Store(directory / "setup.sqlite")
        bound = self.index.get("instance_id")
        if bound is not None and bound != instance_id:
            self.index.close()
            raise ValueError("instance_changed")
        self.index.set("instance_id", instance_id)
        self._finish_eviction()

    def _path(self, value: str) -> Path:
        return self.directory / (connection_id(value) + ".sqlite")

    def _finish_eviction(self):
        with _ACTIVATION:
            self._delete_evicted()

    def _delete_evicted(self):
        victim = self.index.get("evict")
        if victim is not None:
            path = self._path(victim)
            for suffix in ("", "-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            self.index.set("evict", None)

    def prepare(self, code: str, now: int) -> str:
        if not re.fullmatch(r"nsp_[0-9a-f]{64}", code):
            raise ValueError("invalid_code")
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.index.transaction():
            pending = {key: value for key, value in self.index.get("pending", {}).items() if value["expires"] > now}
            if digest not in pending:
                if len(pending) >= 5:
                    raise ValueError("pairing_limit")
                pending[digest] = {"key": "nsg_" + secrets.token_hex(32), "expires": now + 600_000}
            self.index.set("pending", pending)
            return pending[digest]["key"]

    def activate(self, value: str, key: str, now: int, active_ids: list[str]) -> Store:
        with _ACTIVATION:
            return self._activate(value, key, now, active_ids)

    def _activate(self, value: str, key: str, now: int, active_ids: list[str]) -> Store:
        value = connection_id(value)
        profiles = self.index.get("profiles", [])
        profiles = [p for p in profiles if p["id"] != value]
        if len(profiles) >= 5:
            unused = [p for p in profiles if p["id"] not in active_ids]
            if not unused:
                raise ValueError("connection_limit")
            victim = min(unused, key=lambda p: p["used"])
            archive = self.open(victim["id"])
            try:
                archive.seal(now, True)
                counters = archive.status()
                discarded = counters["queued_observations"] + counters["gap_observations"]
            finally:
                archive.close()
            with self.index.transaction():
                self.index.set("discarded", self.index.get("discarded", 0) + discarded)
                self.index.set("evict", victim["id"])
                self.index.set("profiles", [p for p in profiles if p["id"] != victim["id"]])
            self._finish_eviction()
            profiles = [p for p in profiles if p["id"] != victim["id"]]
        profile = self.open(value)
        try:
            with profile.transaction():
                profile.set("instance_id", self.instance_id)
                profile.set("key", key)
                profile.set("connection_id", value)
                profile.set("generation", profile.get("generation", 0) + 1)
                profile.set("selected", [])
                profile.set("control", {"attempts": 0, "retry_at": 0, "failure": None})
            self.index.set("profiles", [*profiles, {"id": value, "used": now}])
            return profile
        except BaseException:
            profile.close()
            raise

    def open(self, value: str) -> Store:
        profile = Store(self._path(value))
        bound = profile.get("instance_id")
        if bound is not None and bound != self.instance_id:
            profile.close()
            raise ValueError("instance_changed")
        return profile

    def prune(self, now: int):
        for row in self.index.get("profiles", []):
            profile = self.open(row["id"])
            try:
                profile.seal(now)
            finally:
                profile.close()

    def remove(self, value: str, now: int, last_entry: bool):
        """HAs uttrykkelige sletting fjerner privat nøkkel og kø, aldri mottatt historikk."""
        with _ACTIVATION:
            targets = [row["id"] for row in self.index.get("profiles", [])] if last_entry else [connection_id(value)]
            for identifier in targets:
                profile = self.open(identifier)
                try:
                    profile.seal(now, True)
                    discarded = profile.status()["queued_observations"]
                finally:
                    profile.close()
                with self.index.transaction():
                    self.index.set("discarded", self.index.get("discarded", 0) + discarded)
                    self.index.set("evict", identifier)
                    self.index.set("profiles", [row for row in self.index.get("profiles", []) if row["id"] != identifier])
                self._finish_eviction()
            if last_entry:
                with self.index.transaction():
                    self.index.set("pending", {})
                    self.index.set("instance_id", None)

    def close(self):
        self.index.close()
