"""Felles grenser; mottaket settes fra Naustos offentlige profil ved pakking."""

DOMAIN = "nausto"
VERSION = "0.4.0"
ENDPOINT = "https://xhlmyytzkiwipacspjsg.supabase.co/functions/v1/sensor-ingest"
PAIR_ENDPOINT = "https://nausto.no/integrasjoner/sensors/pair"
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
# Serveren tar høyst 120 forespørsler i en klokketime per tilkobling og stenger den i en time over
# det. Med minst 31 sekunder mellom forespørslene sender adapteren høyst 117 i en time, også når
# nettets forsinkelse varierer.
REQUEST_INTERVAL_MS = 31_000
# Den faste runden er litt lengre enn mellomrommet. Var den like lang eller kortere, ville
# annenhver runde stoppe på mellomrommet, og livstegnene ville ta plassen til minuttbatchene.
INTERVAL_SECONDS = 33
