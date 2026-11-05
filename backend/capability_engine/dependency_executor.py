"""Execute only API requests described by a loaded configuration record."""
from __future__ import annotations

import json


def execute_dependency(dependency: dict, *, login: dict, inputs: dict, session, url_builder, timeout: int = 60):
    """Execute a configured API dependency using the existing URL builder."""
    normalize = lambda key: "".join(ch for ch in str(key).casefold() if ch.isalnum())
    lowered = {normalize(k): v for k, v in dependency.items()}
    endpoint = next((lowered[k] for k in ("endpoint", "url", "path", "endpointurl", "apiendpoint", "apipath") if lowered.get(k)), None)
    method = str(next((lowered[k] for k in ("method", "httpmethod", "requestmethod") if lowered.get(k)), "POST")).upper()
    if not endpoint or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("Configured API dependency requires a supported method and endpoint.")
    headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
    params = lowered.get("params", lowered.get("queryparams"))
    body = next((lowered[k] for k in ("requestbody", "requestbodyjson", "payload", "requestpayload", "requestjson", "criteria", "body") if k in lowered), None)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
    response = session.request(
        method, url_builder(endpoint, login), headers=headers,
        params=resolve_template(params, login, inputs),
        json=resolve_template(body, login, inputs) if body is not None else None,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def resolve_template(value, login: dict, inputs: dict):
    """Resolve literal JSON templates with ${login.path} / ${field.path} references."""
    if isinstance(value, dict):
        return {k: resolve_template(v, login, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_template(v, login, inputs) for v in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        namespace, _, path = value[2:-1].partition(".")
        root = login if namespace == "login" else inputs if namespace in {"field", "fields"} else None
        if root is None:
            raise ValueError(f"Unsupported configured template reference: {value}")
        for part in path.split("."):
            if not isinstance(root, dict) or part not in root:
                raise ValueError(f"Missing configured template value: {value}")
            root = root[part]
        return root
    return value

