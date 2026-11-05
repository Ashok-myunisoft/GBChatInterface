"""Generic nested JSON payload construction from database mapping rows."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Callable

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _path_tokens(path: str):
    tokens = []
    for name, index in _TOKEN.findall(path):
        tokens.append(int(index) if index else name)
    if not tokens:
        raise ValueError(f"Invalid payload target path: {path!r}")
    return tokens


def _read_path(root: Any, path: str):
    value = root
    for token in path.split("."):
        if isinstance(value, dict) and token in value:
            value = value[token]
        else:
            return _MISSING
    return value


_MISSING = object()


def _assign(root: Any, path: str, value: Any):
    parts = _path_tokens(path)
    current = root
    for i, token in enumerate(parts):
        last = i == len(parts) - 1
        next_token = None if last else parts[i + 1]
        if isinstance(token, int):
            if not isinstance(current, list):
                raise ValueError(f"Array index in {path!r} does not follow an array.")
            while len(current) <= token:
                current.append(None)
            if last:
                current[token] = value
            else:
                if current[token] is None:
                    current[token] = [] if isinstance(next_token, int) else {}
                current = current[token]
        else:
            if not isinstance(current, dict):
                raise ValueError(f"Object field in {path!r} does not follow an object.")
            if last:
                current[token] = value
            else:
                if token not in current or current[token] is None:
                    current[token] = [] if isinstance(next_token, int) else {}
                current = current[token]


def _column(row: dict, *candidates: str, default=None):
    norm = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items()}
    return next((norm[re.sub(r"[^a-z0-9]", "", c.lower())] for c in candidates if re.sub(r"[^a-z0-9]", "", c.lower()) in norm), default)


def build_payload(mappings: list[dict], *, login: dict, fields: dict,
                  resolvers: dict[str, Callable], computations: dict[str, Callable],
                  department_details: dict | None = None) -> dict:
    payload: dict = {}
    for row in mappings:
        target = _column(row, "targetpath", "payloadpath", "targetfield", "fieldpath")
        source_type = str(_column(row, "sourcetype", default="")).upper()
        required = bool(_column(row, "isrequired", "required", default=False))
        if not target:
            raise ValueError("Payload mapping is missing its target path.")
        if source_type == "LOGIN":
            source = _column(row, "sourcefield", "sourcepath", "sourcevalue", "source")
            value = _read_path(login, str(source or ""))
        elif source_type == "FIELD":
            source = _column(row, "sourcefield", "sourcepath", "sourcevalue", "source")
            value = _read_path(fields, str(source or ""))
        elif source_type == "CONSTANT":
            source = _column(row, "sourcevalue", "sourcefield", "source")
            # PostgreSQL JSON/JSONB drivers return the original Python JSON type.
            value = source
        elif source_type == "RESOLVER":
            source = _column(row, "resolvercode", "sourcefield", "sourcevalue", "source")
            resolver = resolvers.get(str(source).upper())
            if not resolver:
                raise ValueError(f"Unregistered resolver: {source}")
            value = resolver()
        elif source_type == "COMPUTED":
            source = _column(row, "sourcefield", "sourcepath", "sourcevalue", "source")
            computation = computations.get(str(source).upper())
            if not computation:
                raise ValueError(f"Unregistered computation: {source}")
            value = computation()
        else:
            raise ValueError(f"Unsupported payload source type: {source_type!r}")
        if value is _MISSING:
            if required:
                raise ValueError(f"Required payload source {source!r} is missing for {target}.")
            continue
        if isinstance(value, Decimal):
            value = float(value)
        _assign(payload, str(target), value)
    if department_details:
        payload.update(department_details)
    return payload

