"""Både synlig kode og Naustos kopierte paringsinformasjon bruker samme felt."""

import json
import re

from .profiles import connection_id, pairing_code


def read_pairing(value: str, endpoint: str) -> tuple[str, str | None]:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid_code")
    value = value.strip()
    normalized = pairing_code(value)
    if normalized is not None:
        return normalized, None
    try:
        data = json.loads(value)
        if (not isinstance(data, dict) or set(data) != {"kind", "version", "endpoint", "connection_id", "code"}
                or data["kind"] != "nausto-sensor-pairing" or type(data["version"]) is not int or data["version"] != 1
                or data["endpoint"] != endpoint or not isinstance(data["code"], str)
                or not re.fullmatch(r"nsp_[0-9a-f]{64}", data["code"])):
            raise ValueError("invalid_code")
        return data["code"], connection_id(data["connection_id"])
    except (ValueError, TypeError):
        raise ValueError("invalid_code") from None
