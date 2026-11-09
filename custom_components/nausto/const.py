"""Felles grenser; mottaket settes fra Naustos offentlige profil ved pakking."""

DOMAIN = "nausto"
VERSION = "0.1.0"
ENDPOINT = "https://xhlmyytzkiwipacspjsg.supabase.co/functions/v1/sensor-ingest"
MAX_ENTITIES = 64
MAX_SOURCES = 16
MAX_OBSERVATIONS = 100
MAX_BYTES = 65536
QUEUE_OBSERVATIONS = 9900
QUEUE_BYTES = 10 * 1024 * 1024 - MAX_BYTES
RETENTION_SECONDS = 7 * 86400
MAX_ATTEMPTS = 12
INTERVAL_SECONDS = 30
