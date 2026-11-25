"""Felles grenser; mottaket settes fra Naustos offentlige profil ved pakking."""

DOMAIN = "nausto"
VERSION = "0.9.0"
# Den faste mottaksadressen (ADR 371). Det tidligere navnet er prosjektets funksjonsadresse, som
# eldre QR-konvolutter fra Nausto har; de gjelder det samme mottaket.
ENDPOINT = "https://nausto.no/integrasjoner/sensors/ingest"
LEGACY_ENDPOINT = "https://xhlmyytzkiwipacspjsg.supabase.co/functions/v1/sensor-ingest"
PAIR_ENDPOINT = "https://nausto.no/integrasjoner/sensors/pair"
INGEST_ENDPOINT = "https://nausto.no/integrasjoner/sensors/ingest"
INGEST_PATH = "/integrasjoner/sensors/ingest"
MAX_ENTITIES = 64
MAX_SOURCES = 16
MAX_OBSERVATIONS = 100
MAX_BYTES = 65536
CONTROL_BYTES = 65536
CONTROL_OBSERVATIONS = 32
# Det åpne målevinduet og kontrollsporet har hver sin faste reserve i samme profil.
QUEUE_OBSERVATIONS = 10000 - MAX_OBSERVATIONS - CONTROL_OBSERVATIONS
QUEUE_BYTES = 10 * 1024 * 1024 - MAX_BYTES - CONTROL_BYTES
RETENTION_SECONDS = 7 * 86400
MAX_ATTEMPTS = 12
# Serveren tar høyst 240 forespørsler i en klokketime per tilkobling og stenger den i en time over
# det. Med minst 31 sekunder mellom forespørslene sender adapteren høyst 117 i en time, godt under
# taket også når nettets forsinkelse varierer.
REQUEST_INTERVAL_MS = 31_000
# Den faste runden er litt lengre enn mellomrommet. Var den like lang eller kortere, ville
# annenhver runde stoppe på mellomrommet, og livstegnene ville ta plassen til minuttbatchene.
INTERVAL_SECONDS = 33

# Prioritert sendevei og liveforbindelse (ADR 320). Verdiene er de samme som i
# supabase/functions/_shared/sensor-live-contract.ts, og prøven sammenligner dem.
PRIORITY_HEADER = "X-Nausto-Priority"
PRIORITY_LIMITED_HEADER = "X-Nausto-Priority-Limited"
LIVE_HEADER = "X-Nausto-Live"
# Adapterens tak per rullerende time. Serverens er 120 og 60 per klokketime, og ingen prioritert
# forespørsel teller mot de 120 på den vanlige veien.
PRIORITY_LIMITS = {"control": 100, "alarm": 50}
# Handlingene hvert spor tar. Alt annet, også tilbud, går den vanlige veien.
PRIORITY_ACTIONS = {"control": ("heartbeat", "control_state", "control_result"), "alarm": ("batch",)}
# En kilderapport etter en utført kommando er en bekreftelse og tar styringssporet så lenge.
CONTROL_CONFIRMATION_MS = 60_000
LIVE_PROTOCOL = 1
LIVE_PATH = "/integrasjoner/sensors/live"
LIVE_SUBPROTOCOL = "nausto-live.v1"
LIVE_TICKET_PROTOCOL_PREFIX = "nausto-ticket."
LIVE_TICKET_SECONDS = 600
LIVE_RENEW_SECONDS = 300
LIVE_RETRY_SECONDS = 600
LIVE_PING = '{"type":"ping"}'
LIVE_PONG = '{"type":"pong"}'
LIVE_PING_SECONDS = 45
LIVE_MESSAGE_BYTES = 1024
# Hele lista fra kontrakten. Adapteren handler særskilt bare på utløp, erstatning og taket; resten,
# også 4029 for for mange liveverdier, gir ny tilkobling med vanlig ventetid.
LIVE_CLOSE = {"expired": 4001, "protocol": 4002, "access": 4003, "replaced": 4008, "limit": 4009,
              "idle": 4010, "rate": 4029}
LIVE_WAKE = '{"type":"wake","reason":"control"}'
# Livevisningen (del 2 av #2295): mens serveren sier «på», går hver ny, datert observasjon over
# forbindelsen, også når batchen venter med den.
LIVE_ON = '{"type":"live","on":true}'
LIVE_OFF = '{"type":"live","on":false}'
LIVE_VALUES_EVERY_MS = 1000
LIVE_ADAPTER_BURST = 30
LIVE_VALUES_WINDOW_MS = 10_000
LIVE_VALUES_MAX = 64
LIVE_VALUES_BYTES = 16 * 1024

# Adapterens egne valg innenfor kontrakten.
# Høyst én billettforespørsel i minuttet, også når svarene feiler.
LIVE_ASK_SECONDS = 60
# Med mindre igjen av billetten kobler adapteren ikke til; den venter på en ny.
LIVE_MIN_SECONDS = 30
# Gjenoppkobling etter brudd: full tilfeldig spredning fra ett sekund, doblet til ett minutt.
LIVE_BACKOFF_SECONDS = (1, 60)
# Båten har nådd taket for samtidige forbindelser (4009).
LIVE_LIMIT_PAUSE_SECONDS = 600
LIVE_CONNECT_SECONDS = 15
LIVE_CLOSE_SECONDS = 5
# Et spor venter selv etter nettfeil, 503 og avslag: fem sekunder, doblet til fem minutter.
LANE_BACKOFF_SECONDS = (5, 300)
LANE_WINDOW_SECONDS = 3600
# Serveren tar en overgang som er høyst fem minutter gammel; adapteren har ett minutts margin.
ALARM_LANE_MS = 240_000
# En vekking gjelder en kommando som utløper innen ett minutt; deretter hentes den vanlig vei.
WAKE_MS = 60_000
