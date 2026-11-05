"""Load and validate a capability version, with explicit DRAFT test mode."""
from __future__ import annotations

import json
import os

from .capability_repository import CapabilityRepository


def load_capability(code: str, test_mode: bool | None = None) -> dict:
    if test_mode is None:
        test_mode = os.getenv("CAPABILITY_ENGINE_TEST_MODE", "false").strip().lower() in {"1", "true", "yes"}
    record = CapabilityRepository().load_by_code(code)
    if not record:
        raise RuntimeError(f"Capability {code} is not configured.")
    version = record["version"]
    status = next((str(v).upper() for k, v in version.items() if k.lower() in {"status", "versionstatus"}), "")
    if status in {"DRAFT", "IN_REVIEW"} and not test_mode:
        raise RuntimeError(f"Capability {code} has no production-active version.")
    raw = next((v for k, v in version.items() if k.lower() == "configurationjson"), None)
    if isinstance(raw, str):
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Capability {code} configuration JSON is invalid.") from exc
    elif isinstance(raw, dict):
        config = raw
    else:
        config = {}
    if not isinstance(config, dict):
        raise RuntimeError(f"Capability {code} configuration must be a JSON object.")
    record["dependencies"] = next(
        (value for key, value in config.items()
         if key.casefold() in {"dependencies", "apidependencies", "api_dependencies", "apis"}),
        [],
    )
    if isinstance(record["dependencies"], dict):
        record["dependencies"] = list(record["dependencies"].values())
    if not isinstance(record["dependencies"], list):
        raise RuntimeError(f"Capability {code} API dependency configuration must be a list.")
    if code.upper() == "APPLY_LEAVE":
        expected = {"fields": 8, "validations": 5, "payloads": 39}
        for section, count in expected.items():
            if len(record.get(section, [])) != count:
                raise RuntimeError(
                    f"APPLY_LEAVE configuration is incomplete: expected {count} active {section}, "
                    f"found {len(record.get(section, []))}."
                )
        if len(record["dependencies"]) != 4:
            raise RuntimeError(
                f"APPLY_LEAVE configuration is incomplete: expected 4 API dependencies, "
                f"found {len(record['dependencies'])}."
            )
    return record

