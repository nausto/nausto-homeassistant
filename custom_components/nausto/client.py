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
    def __init__(self, session: aiohttp.ClientSession, endpoint: str | None = None):
        self.session = session
        self.endpoint = endpoint or const.ENDPOINT
        url = urlsplit(self.endpoint)
        if (url.username or url.password or url.query or url.fragment or
                url.path != "/functions/v1/sensor-ingest" or
                not (url.scheme == "https" or (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")))):
            raise ValueError("invalid_endpoint")

    async def send(self, key: str, message: str) -> Receipt:
        try:
            async with self.session.post(
                self.endpoint, data=message.encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
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
                retry = min(3600, int(retry_text)) if retry_text.isdecimal() else 0
                action = json.loads(message)["action"]
                expected = ("paired",) if action == "pair" else ("accepted", "duplicate") if action == "batch" else ("accepted",)
                return Receipt(response.status == 200 and data is not None and data.get("status") in expected,
                               response.status, retry, data)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeDecodeError):
            return Receipt(False, 503, 0, None)
