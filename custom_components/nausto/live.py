"""Prioritert sendevei og liveforbindelse (ADR 320), med kontrakten i sensor-live-contract.ts.

Forbindelsen bærer vekking og, mens serveren sier «på», hver ny, datert observasjon, også dem
batchen venter med (livevisningen, del 2 av #2295). Kommandoer, kvitteringer, kilderapporter og
alarmoverganger går alltid gjennom HTTPS til sensor-ingest, så ett varig registrert fysisk forsøk
gjelder uendret, og historikken kommer bare fra batchene. Billetten logges, lagres og vises aldri,
og den står aldri i adressen.
"""

import asyncio
from collections import deque
import json
import math
import random
import re
import time
from urllib.parse import urlsplit

import aiohttp

from . import const
from .store import encoded

TICKET = re.compile(r"v1\.[A-Za-z0-9_-]{16,400}\.[A-Za-z0-9_-]{43}")
SAFE_INTEGER = 2 ** 53 - 1
# Livevisningens tider i sekunder på adapterens monotone klokke.
VALUES_EVERY = const.LIVE_VALUES_EVERY_MS / 1000
VALUES_WINDOW = const.LIVE_VALUES_WINDOW_MS / 1000
VALUES_WRAPPER = '{"type":"values","values":[]}'


def _integer(value, low: int, high: int) -> bool:
    """Som Number.isSafeInteger i kontrakten: 600.0 fra JSON godtas, true gjør det ikke."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return abs(value) <= SAFE_INTEGER and low <= value <= high


def is_ticket(value) -> bool:
    return isinstance(value, str) and TICKET.fullmatch(value) is not None


def protocols(ticket: str) -> tuple[str, str]:
    """Protokollene adapteren tilbyr i håndtrykket; serveren velger bare nausto-live.v1."""
    if not is_ticket(ticket):
        raise ValueError("invalid_live_ticket")
    return const.LIVE_SUBPROTOCOL, const.LIVE_TICKET_PROTOCOL_PREFIX + ticket


def live_url(pair_endpoint) -> str | None:
    """Adressen avledes fra den faste paringsadressen, aldri fra en adresse serveren sender."""
    try:
        url = urlsplit(pair_endpoint)
        port = url.port
    except (AttributeError, TypeError, ValueError):
        return None
    host = url.hostname
    if (not host or url.path != "/integrasjoner/sensors/pair" or url.query or url.fragment or
            url.username or url.password):
        return None
    if url.scheme == "https":
        scheme, default = "wss", 443
    elif url.scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        scheme, default = "ws", 80
    else:
        return None
    netloc = (f"[{host}]" if ":" in host else host) + ("" if port in (None, default) else f":{port}")
    return f"{scheme}://{netloc}{const.LIVE_PATH}"


def read_grant(data):
    """Billetten i et godtatt svar på X-Nausto-Live: 1.

    None sier ingenting om live (eldre server eller ikke spurt), "off" at serveren følger
    kontrakten, men ikke gir billett nå, og ellers (billett, levetid i millisekunder). Levetiden er
    relativ, så en adapterklokke som går feil, ikke flytter fornyelsen."""
    if not isinstance(data, dict) or not _integer(data.get("live_protocol"), const.LIVE_PROTOCOL, const.LIVE_PROTOCOL):
        return None
    ticket, seconds = data.get("live_ticket"), data.get("live_expires_in")
    if not is_ticket(ticket) or not _integer(seconds, 60, const.LIVE_TICKET_SECONDS):
        return "off"
    return ticket, int(seconds) * 1000


def _constant(value):
    # JSON.parse i kontrakten tar ikke NaN eller Infinity.
    raise ValueError("invalid_json")


def read_message(raw) -> str | None:
    """"wake", "pong", "on", "off" eller None. Ukjente meldinger ignoreres, så en nyere server kan
    legge til typer.

    Som i kontrakten godtas ekstra felt, men «på» og «av» krever en ekte bool, ikke 1 eller "true".
    Lengden telles i UTF-16-enheter som JavaScripts length i kontrakten. Forbindelsen slipper
    uansett ikke gjennom en ramme over 1024 byte, så grensen her er aldri den strengeste."""
    if not isinstance(raw, str) or len(raw.encode("utf-16-le", "surrogatepass")) // 2 > const.LIVE_MESSAGE_BYTES:
        return None
    if raw == const.LIVE_PONG:
        return "pong"
    try:
        value = json.loads(raw, parse_constant=_constant)
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("type") == "wake" and value.get("reason") == "control":
        return "wake"
    if isinstance(value, dict) and value.get("type") == "live" and isinstance(value.get("on"), bool):
        return "on" if value["on"] else "off"
    return None


def _split(texts) -> list[list[str]]:
    """Delingen i sensorLiveValueMessages: høyst 64 verdier og 16 KiB UTF-8 per melding."""
    groups, items, size = [], [], len(VALUES_WRAPPER)
    for text in texts:
        length = len(text.encode())
        if items and (len(items) >= const.LIVE_VALUES_MAX or size + 1 + length > const.LIVE_VALUES_BYTES):
            groups.append(items)
            items, size = [], len(VALUES_WRAPPER)
        size += length + (1 if items else 0)
        items.append(text)
    if items:
        groups.append(items)
    return groups


def _message(items) -> str:
    return '{"type":"values","values":[' + ",".join(items) + "]}"


def value_messages(observations) -> list[str]:
    """Som sensorLiveValueMessages, med observasjonene kodet som i batchen."""
    return [_message(group) for group in _split([encoded(value) for value in observations])]


def _number(value) -> bool:
    # Som typeof i kontrakten er en bool ikke et tall.
    return type(value) in (int, float)


class Waiting:
    """En verdi som venter: teksten, om den går straks, om den er et tall og om grensen har holdt den."""

    __slots__ = ("text", "urgent", "number", "held")

    def __init__(self, text: str, urgent: bool, number: bool):
        self.text, self.urgent, self.number, self.held = text, urgent, number, False


class Values:
    """Livevisningens strøm (del 2 av #2295): hva som går, og når. Live sender det på forbindelsen.

    Siste observasjon per kanal står klar til øyeblikksbildet. Mens strømmen går, venter det som
    ikke er sendt, med siste verdi per kanal: tall høyst én melding i sekundet, alt annet straks, og
    høyst 30 meldinger på et glidende vindu på ti sekunder, øyeblikksbildet medregnet. Tidene er
    adapterens monotone klokke i sekunder; det som ikke får plass, venter til vinduet tillater det."""

    def __init__(self, allowed=None):
        # Kanalene batchene tar nå, eller None for alle. Et bortvalg gjelder også det som venter.
        self.allowed = allowed or (lambda: None)
        # Kanal → (kodet observasjon, tall); aldri nye tider eller ID-er.
        self.latest = {}
        self.pending = {}
        self.sent = deque()
        self.numbers_at = -math.inf
        self.on = False
        self.changed = asyncio.Event()
        self.counters = dict.fromkeys(("snapshots", "messages", "values", "coalesced", "held"), 0)

    def add(self, observation):
        """En ny, datert observasjon fra opptaket, med ID-en og kildens tider derfra. Den blir siste
        kjente for kanalen og går live bare mens strømmen er på."""
        try:
            channel, text = observation["channel_id"], encoded(observation)
            fits = len(VALUES_WRAPPER) + len(text.encode()) <= const.LIVE_VALUES_BYTES
        except (KeyError, TypeError, ValueError):
            return
        if not fits:
            return
        number = _number(observation.get("value"))
        self.latest[channel] = text, number
        if len(self.latest) > const.MAX_ENTITIES:
            self._prune()
        if self.on:
            if channel in self.pending:
                self.counters["coalesced"] += 1
            # Bool, tekst og ukjent eller utilgjengelig går straks; tall samles.
            self.pending[channel] = Waiting(text, not number, number)
            self.changed.set()

    def forget(self, channel: str):
        """HA har en udatert verdi: kanalen står utenfor øyeblikksbildet til neste daterte verdi.
        Uten kildens tid ville mottakstiden gjort en gammel verdi ny."""
        self.latest.pop(channel, None)

    def _prune(self):
        allowed = self.allowed()
        if allowed is not None:
            self.latest = {key: value for key, value in self.latest.items() if key in allowed}
        while len(self.latest) > const.MAX_ENTITIES:
            del self.latest[next(iter(self.latest))]

    def start(self):
        """«På»: et nytt øyeblikksbilde, også mens strømmen går, for da har en ny seer kommet til."""
        allowed = self.allowed()
        self.on = True
        self.counters["snapshots"] += 1
        self.pending = {channel: Waiting(text, True, number) for channel, (text, number) in self.latest.items()
                        if allowed is None or channel in allowed}
        self.changed.set()

    def stop(self):
        """«Av», brudd eller ny forbindelse: strømmen stopper, og det som venter, forkastes."""
        self.on = False
        self.pending.clear()

    def _used(self, now: float) -> int:
        while self.sent and now >= self.sent[0] + VALUES_WINDOW:
            self.sent.popleft()
        return len(self.sent)

    def wait(self, now: float) -> float | None:
        """Sekunder til noe kan gå: 0 nå, og None når ingenting venter."""
        if not self.on or not self.pending:
            return None
        due = 0.0 if any(value.urgent for value in self.pending.values()) else self.numbers_at + VALUES_EVERY - now
        if self._used(now) >= const.LIVE_ADAPTER_BURST:
            due = max(due, self.sent[0] + VALUES_WINDOW - now)
        return max(0.0, due)

    def take(self, now: float) -> list[str]:
        """Meldingene som kan gå nå. Resten venter og samles, så siste verdi per kanal kommer fram."""
        if not self.on:
            return []
        allowed = self.allowed()
        if allowed is not None:
            for channel in [key for key in self.pending if key not in allowed]:
                del self.pending[channel]
        numbers = now >= self.numbers_at + VALUES_EVERY
        ready = [(channel, value) for channel, value in self.pending.items() if value.urgent or numbers]
        if not ready:
            return []
        groups = _split([value.text for _, value in ready])[:max(0, const.LIVE_ADAPTER_BURST - self._used(now))]
        count = sum(len(group) for group in groups)
        for _, value in ready[count:]:
            if not value.held:
                value.held = True
                self.counters["held"] += 1
        for channel, value in ready[:count]:
            del self.pending[channel]
            if value.number:
                self.numbers_at = now
        self.sent.extend([now] * len(groups))
        self.counters["messages"] += len(groups)
        self.counters["values"] += count
        return [_message(group) for group in groups]

    def status(self) -> dict:
        """Bare om strømmen går og tellerne; aldri verdiene."""
        return {"streaming": self.on, **{f"stream_{name}": value for name, value in self.counters.items()}}


class Lane:
    """Ett prioritert spor med eget rullerende tak og egen pause, uavhengig av den vanlige veien."""

    def __init__(self, name: str):
        self.name, self.limit = name, const.PRIORITY_LIMITS[name]
        # Sendetidene i den rullerende timen; aldri flere enn taket.
        self.sent = deque()
        self.paused_until = 0.0
        self.backoff = 0.0

    def used(self, now: float) -> int:
        while self.sent and now - self.sent[0] >= const.LANE_WINDOW_SECONDS:
            self.sent.popleft()
        return len(self.sent)

    def wait(self, now: float) -> float:
        """Sekunder til sporet kan sende igjen; 0 når det er ledig."""
        wait = max(0.0, self.paused_until - now)
        if self.used(now) >= self.limit:
            wait = max(wait, self.sent[0] + const.LANE_WINDOW_SECONDS - now)
        return wait

    def count(self, now: float):
        # Taket telles ved sending, uansett utfall.
        self.sent.append(now)

    def accepted(self):
        self.backoff = 0.0

    def failed(self, now: float):
        low, high = const.LANE_BACKOFF_SECONDS
        self.backoff = min(high, max(low, self.backoff * 2))
        self.paused_until = max(self.paused_until, now + self.backoff)

    def limited(self, now: float, seconds: int):
        """Serverens Retry-After for sporet; den vanlige veien sperres ikke."""
        self.paused_until = max(self.paused_until, now + seconds)


class Live:
    """Én utgående forbindelse per tilkobling. Et vekk ber bare om en henting gjennom HTTPS, og
    liveverdiene går bare mens serveren har sagt «på» på den forbindelsen som er i bruk."""

    def __init__(self, session, url: str, wake, monotonic=time.monotonic, sleep=asyncio.sleep, jitter=random.random,
                 allowed=None):
        self.session, self.url, self.wake = session, url, wake
        self.monotonic, self.sleep, self.jitter = monotonic, sleep, jitter
        self.ticket, self.expires = None, 0.0
        self.asked_at, self.quiet_until = None, 0.0
        self.changed = asyncio.Event()
        self.socket, self.socket_ticket = None, None
        self.opened = set()
        self.failures, self.pause_until = 0, 0.0
        self.last_pong = self.next_ping = 0.0
        self.stopped = False
        self.values = Values(allowed)

    @property
    def connected(self) -> bool:
        return self.socket is not None

    def observed(self, observation):
        """En ny, datert observasjon fra opptaket; hendelsesløkka venter aldri på nettet for den."""
        self.values.add(observation)

    def forget(self, channel: str):
        """En udatert verdi i HA: kanalen står utenfor øyeblikksbildet til neste daterte verdi."""
        self.values.forget(channel)

    def should_ask(self, now: float) -> bool:
        """X-Nausto-Live: 1 på en vanlig forespørsel: uten billett eller med under fem minutter igjen,
        høyst én gang i minuttet og ikke i ti minutter etter et svar uten billett."""
        if self.stopped or now < self.quiet_until:
            return False
        if self.ticket is not None and self.expires - now >= const.LIVE_RENEW_SECONDS:
            return False
        return self.asked_at is None or now - self.asked_at >= const.LIVE_ASK_SECONDS

    def asked(self, now: float):
        self.asked_at = now

    def answered(self, data, now: float) -> bool:
        """Et godtatt svar på en forespørsel med billettønske. Sann når serveren følger kontrakten."""
        grant = read_grant(data)
        if not isinstance(grant, tuple):
            # Live er av til neste forsøk. En billett som fortsatt gjelder, beholdes til den går ut.
            self.quiet_until = now + const.LIVE_RETRY_SECONDS
            return grant == "off"
        self.ticket, self.expires = grant[0], now + grant[1] / 1000
        self.changed.set()
        return True

    def _usable(self, now: float) -> bool:
        return self.ticket is not None and self.expires - now > const.LIVE_MIN_SECONDS

    def _forget(self, ticket):
        if self.ticket == ticket:
            self.ticket = None

    def _broken(self):
        # Full tilfeldig spredning fra ett sekund, doblet til ett minutt.
        low, high = const.LIVE_BACKOFF_SECONDS
        self.failures = min(self.failures + 1, 16)
        delay = self.jitter() * min(high, low * 2 ** (self.failures - 1))
        self.pause_until = max(self.pause_until, self.monotonic() + delay)

    def _closed(self, code, ticket):
        close = const.LIVE_CLOSE
        if code in (close["expired"], close["replaced"]):
            # Utgått billett, eller en nyere forbindelse fra samme tilkobling: vent på ny billett.
            # En egen eldre forbindelse leses ikke lenger etter fornyelsen, så den kommer ikke hit.
            self._forget(ticket)
        elif code == close["limit"]:
            self.pause_until = max(self.pause_until, self.monotonic() + const.LIVE_LIMIT_PAUSE_SECONDS)
        else:
            self._broken()

    async def _close(self, socket):
        self.opened.discard(socket)
        try:
            async with asyncio.timeout(const.LIVE_CLOSE_SECONDS):
                await socket.close()
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            pass

    async def _drop(self):
        socket, self.socket, self.socket_ticket = self.socket, None, None
        # Etter et brudd strømmer ingenting før serveren sier «på» på en ny forbindelse.
        self.values.stop()
        if socket is not None:
            await self._close(socket)

    async def _open(self, ticket):
        """En ny forbindelse med billetten i protokollen, eller None etter et brudd."""
        try:
            async with asyncio.timeout(const.LIVE_CONNECT_SECONDS):
                # aiohttp avviser en ramme på akkurat max_msg_size byte, så grensen er én over
                # kontraktens. En lengre ramme bryter forbindelsen før noe av den bufres.
                socket = await self.session.ws_connect(
                    self.url, protocols=protocols(ticket), heartbeat=None, autoping=True,
                    max_msg_size=const.LIVE_MESSAGE_BYTES + 1)
        except aiohttp.WSServerHandshakeError as error:
            if 400 <= error.status < 500:
                # Serveren godtar ikke billetten; en ny kommer med en vanlig forespørsel.
                self._forget(ticket)
            else:
                self._broken()
            return None
        except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError):
            self._broken()
            return None
        self.opened.add(socket)
        if self.stopped or socket.protocol != const.LIVE_SUBPROTOCOL:
            await self._close(socket)
            if not self.stopped:
                self._broken()
            return None
        now = self.monotonic()
        self.last_pong, self.next_ping = now, now + const.LIVE_PING_SECONDS
        return socket

    async def _read(self):
        socket, now = self.socket, self.monotonic()
        if now >= self.next_ping:
            if now - self.last_pong >= 2 * const.LIVE_PING_SECONDS:
                # Ingen pong på to intervaller: forbindelsen er død og kobles til på nytt.
                await self._drop()
                self._broken()
                return
            await socket.send_str(const.LIVE_PING)
            self.next_ping = now + const.LIVE_PING_SECONDS
            return
        try:
            message = await socket.receive(timeout=self.next_ping - now)
        except asyncio.TimeoutError:
            return
        kind = message.type
        if kind == aiohttp.WSMsgType.TEXT:
            value = read_message(message.data)
            if value == "pong":
                self.last_pong, self.failures = self.monotonic(), 0
            elif value == "wake":
                self.wake()
            elif value == "on":
                self.values.start()
            elif value == "off":
                self.values.stop()
        elif kind in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                      aiohttp.WSMsgType.ERROR):
            # Lukkekoden fra serveren, eller 1006 når forbindelsen bare forsvant.
            code, ticket = socket.close_code, self.socket_ticket
            await self._drop()
            self._closed(code, ticket)

    async def step(self):
        now = self.monotonic()
        if self.socket is None:
            if now < self.pause_until:
                await self.sleep(self.pause_until - now)
            elif not self._usable(now):
                self.changed.clear()
                await self.changed.wait()
            else:
                ticket = self.ticket
                socket = await self._open(ticket)
                if socket is not None:
                    self.values.stop()
                    self.socket, self.socket_ticket = socket, ticket
            return
        if self.ticket != self.socket_ticket and self._usable(now) and now >= self.pause_until:
            # Fornyelse er ny tilkobling; den gamle lukkes først når den nye er åpen. Den nye
            # strømmer ikke før serveren sier «på» på den, og det kommer straks om noen ser på.
            ticket = self.ticket
            newer = await self._open(ticket)
            if newer is not None:
                self.values.stop()
                older, self.socket, self.socket_ticket = self.socket, newer, ticket
                await self._close(older)
            return
        await self._read()

    async def flush(self):
        """Sender det strømmen har klart nå, på forbindelsen som sa «på»."""
        socket = self.socket
        if socket is None:
            self.values.stop()
            return
        for message in self.values.take(self.monotonic()):
            if self.socket is not socket or not self.values.on:
                return
            await socket.send_str(message)

    async def _idle(self, delay: float | None):
        """Til en ny verdi eller «på» kommer, eller til fristen; lesingen går imens."""
        tasks = [asyncio.ensure_future(self.values.changed.wait())]
        if delay is not None:
            tasks.append(asyncio.ensure_future(self.sleep(delay)))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()

    async def _send_values(self):
        """Liveverdiene går ved siden av lesingen, så en ramme som venter, aldri holder dem igjen."""
        values = self.values
        while not self.stopped:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                # En brutt forbindelse ses av lesingen, som kobler til på nytt; ingen feiltekst i HAs logg.
                values.stop()
            # Ventetiden regnes og hendelsen nullstilles uten et await imellom, så ingen verdi går tapt.
            delay = values.wait(self.monotonic())
            values.changed.clear()
            await self._idle(delay)

    async def run(self):
        """Holder forbindelsen så lenge billetten gjelder, til stop()."""
        sender = asyncio.ensure_future(self._send_values())
        try:
            while not self.stopped:
                try:
                    await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Ingen feiltekst går til HAs logg; gjenoppkobling med spredning.
                    await self._drop()
                    self._broken()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            await self._drop()
            for socket in list(self.opened):
                await self._close(socket)

    async def stop(self, task):
        self.stopped = True
        self.changed.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
