"""Avgrenset HTTPS med eksplisitt kvittering; ingen råfeil sendes videre til HA."""

import asyncio
from dataclasses import dataclass
import json
from urllib.parse import urlsplit

import aiohttp

from . import const


@dataclass(frozen=True)
class Receipt:
    ok: bool
    status: int
    retry: int
    data: dict | None


class Client:
    def __init__(self, session: aiohttp.ClientSession, endpoint: str | None = None, pair_endpoint: str | None = None):
        self.session = session
        self.endpoint = endpoint or const.ENDPOINT
        url = urlsplit(self.endpoint)
        if (url.username or url.password or url.query or url.fragment or
                url.path != "/functions/v1/sensor-ingest" or
                not (url.scheme == "https" or (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")))):
            raise ValueError("invalid_endpoint")
        self.pair_endpoint = pair_endpoint or const.PAIR_ENDPOINT
        pair = urlsplit(self.pair_endpoint)
        if (pair.username or pair.password or pair.query or pair.fragment or
                pair.path != "/integrasjoner/sensors/pair" or
                not (self.pair_endpoint == "https://nausto.no/integrasjoner/sensors/pair" or
                     pair.scheme == "http" and pair.hostname in ("127.0.0.1", "localhost", "::1") and
                     url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1"))):
            raise ValueError("invalid_pair_endpoint")

    async def send(self, key: str, message: str, control_nonce: str | None = None) -> Receipt:
        try:
            action = json.loads(message)["action"]
            short_pair = key.startswith("nsc_")
            if short_pair and action != "pair":
                return Receipt(False, 400, 0, None)
            headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
            if control_nonce is not None:
                headers["X-Nausto-Control-Nonce"] = control_nonce
            async with self.session.post(
                self.pair_endpoint if short_pair else self.endpoint, data=message.encode(),
                headers=headers,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                chunks, length = [], 0
                while chunk := await response.content.read(8192):
                    length += len(chunk)
                    if length > const.MAX_BYTES:
                        response.close()
                        return Receipt(False, response.status, 0, None)
                    chunks.append(chunk)
                try:
                    data = json.loads(b"".join(chunks))
                    if not isinstance(data, dict):
                        data = None
                except (ValueError, UnicodeDecodeError):
                    data = None
                retry_text = response.headers.get("Retry-After", "0")
                retry = min(600 if short_pair else 3600, int(retry_text)) if retry_text.isdecimal() else 0
                expected = ("paired",) if action == "pair" else ("accepted", "duplicate") if action == "batch" else ("accepted",)
                return Receipt(response.status == 200 and data is not None and data.get("status") in expected,
                               response.status, retry, data)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeDecodeError):
            return Receipt(False, 503, 0, None)
