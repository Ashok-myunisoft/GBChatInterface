from __future__ import annotations

import base64
import gzip
import zlib
import json
from typing import Any


class EnvelopeError(Exception):
    pass


class GB4ApiError(Exception):
    def __init__(self, message: str, error_number=None, error_type=None):
        self.error_number = error_number
        self.error_type = error_type
        super().__init__(message)


def _decode_body(body: Any) -> Any:
    if not isinstance(body, str):
        return body

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

    try:
        decoded = base64.b64decode(body)
    except Exception:
        return body

    try:
        decompressed = gzip.decompress(decoded)
        return json.loads(decompressed)
    except Exception:
        pass

    try:
        decompressed = zlib.decompress(decoded)
        return json.loads(decompressed)
    except Exception:
        pass

    try:
        decompressed = zlib.decompressobj(-zlib.MAX_WBITS).decompress(decoded)
        return json.loads(decompressed)
    except Exception:
        pass

    return body


def unwrap(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise EnvelopeError(f"Response is not JSON at all: {e}") from e

    for _ in range(5):
        if not isinstance(raw, dict):
            return raw

        if raw.get("ErrorNumber") or raw.get("ErrorType") not in (None, 0):
            raise GB4ApiError(
                raw.get("Body") or "GoodBooks returned an error.",
                error_number=raw.get("ErrorNumber"),
                error_type=raw.get("ErrorType"),
            )

        if "Body" not in raw and isinstance(raw.get("contents"), dict):
            raw = raw["contents"]
            continue

        if "Body" not in raw:
            return raw

        body = raw["Body"]
        if body is None:
            return None

        decoded = _decode_body(body)

        raw = decoded

    return raw


def raw_body_string(raw: Any) -> str | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, dict):
        if "Body" not in raw and isinstance(raw.get("contents"), dict):
            raw = raw["contents"]
        return raw.get("Body")
    return None