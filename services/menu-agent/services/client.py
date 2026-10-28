from __future__ import annotations

import json
import sys
from urllib.parse import quote

import requests

from menu_agent_config import settings
from services.envelope import unwrap, EnvelopeError, GB4ApiError


class GoodBooksSessionError(Exception):
    pass


class GoodBooksClient:
    def __init__(self, login_header: str, base_url: str | None = None):
        if not login_header:
            raise GoodBooksSessionError("Missing Login header/session blob.")
        try:
            login = json.loads(login_header)
        except json.JSONDecodeError as e:
            raise GoodBooksSessionError(f"Login header is not valid JSON: {e}") from e
        if not isinstance(login, dict):
            raise GoodBooksSessionError("Login header must be a JSON object.")

        self.login_header = login_header
        self.login = login

        self._explicit_base_url = (base_url or "").rstrip("/") or None
        self._base_uri = (login.get("BaseUri") or "").strip()
        self._fe_uri = (login.get("FEUri") or "").strip().rstrip("/")
        if not self._explicit_base_url and not (self._base_uri and self._fe_uri):
            raise GoodBooksSessionError(
                "Could not determine how to reach GoodBooks: the Login blob "
                "has neither BaseUri nor FEUri, and GOODBOOKS_BASE_URL isn't "
                "set."
            )

        self.user_id = login.get("UserId")
        self.user_code = login.get("UserCode")
        self.user_name = login.get("UserName", self.user_code)
        self.source_type = login.get("SourceType", 0)

        self.session = requests.Session()

    def _url(self, service_path: str) -> str:
        service_path = service_path.lstrip("/")

        if self._explicit_base_url:
            return f"{self._explicit_base_url}/gb4/fws/{service_path}"

        target = f"http://{self._base_uri}/gb4/fws/{service_path}"
        return f"{self._fe_uri}/apps/proxy.php?url={quote(target, safe='')}"

    def _request(self, method: str, service_path: str, json_body=None):
        url = self._url(service_path)
        headers = {"Login": self.login_header}
        resp = self.session.request(
            # Was 30s. Root cause of "ReadTimeout ... read timeout=30" on
            # otherwise-valid saves: GoodBooks (192.168.0.112) sometimes
            # takes longer than 30s to process a save under load, and
            # requests aborts the connection before a response ever comes
            # back -- this is a client-side cutoff, not a GoodBooks error.
            # Raised to 90s to give slower saves room to complete.
            method=method.upper(), url=url, json=json_body, headers=headers, timeout=90
        )
        if resp.status_code in (401, 403):
            raise GoodBooksSessionError(
                f"GoodBooks rejected the Login session ({resp.status_code}) on "
                f"{service_path}. It's likely expired -- log in again in the "
                "browser/Task bot and pass the fresh Login header."
            )
        resp.raise_for_status()

        try:
            raw = resp.json()
        except ValueError:
            raw = resp.text

        try:
            return unwrap(raw)
        except GB4ApiError as e:
            if e.error_number == "UNI0013":
                raise GoodBooksSessionError(
                    f"GoodBooks session has expired on {service_path} "
                    f"(ErrorNumber={e.error_number}). This IS a login/"
                    "session problem -- log in again in the browser/Task "
                    "bot and pass the fresh Login header."
                ) from e
            raise GoodBooksSessionError(
                f"GoodBooks rejected the request on {service_path}: {e} "
                f"(ErrorNumber={e.error_number}). This is a GoodBooks-side "
                "error on the data you submitted (e.g. a foreign-key/"
                "constraint conflict), NOT a login/session problem -- "
                "logging in again will not fix it. Check which resolved "
                "id/value in this request GoodBooks is rejecting."
            ) from e
        except EnvelopeError:
            return raw

    def get(self, service_path: str):
        return self._request("GET", service_path)

    def post(self, service_path: str, json_body: dict):
        return self._request("POST", service_path, json_body)

    def delete(self, service_path: str):
        return self._request("DELETE", service_path)


client = GoodBooksClient(
    login_header=settings.GOODBOOKS_LOGIN_JSON,
    base_url=settings.GOODBOOKS_BASE_URL or None,
)