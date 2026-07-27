"""Tdarr v2 REST client. Dumb transport.

Auth: if username/password are configured we POST /api/v2/public/auth/login for a Bearer token;
otherwise we assume the instance has auth disabled and send no token.
"""

from __future__ import annotations

import requests

from . import log
from .errors import TransientError
from .models import TdarrConfig

_log = log.get("tdarr")

DEFAULT_SCAN_MODE = "scanFolderWatcher"


class TdarrClient:
    def __init__(
        self,
        config: TdarrConfig,
        *,
        scan_mode: str = DEFAULT_SCAN_MODE,
        session: requests.Session | None = None,
    ) -> None:
        self._cfg = config
        self.scan_mode = scan_mode
        self._session = session or requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._token: str | None = None
        self._authed = False

    # --- auth ---------------------------------------------------------------

    @property
    def auth_enabled(self) -> bool:
        return bool(self._cfg.username and self._cfg.password)

    def ensure_auth(self) -> None:
        if self._authed or not self.auth_enabled:
            self._authed = True
            return
        url = f"{self._cfg.url}/api/v2/public/auth/login"
        try:
            resp = self._session.post(
                url,
                json={"username": self._cfg.username, "password": self._cfg.password},
                timeout=self._cfg.request_timeout_seconds,
            )
            resp.raise_for_status()
            token = resp.json().get("token")
        except requests.RequestException as exc:
            raise TransientError(f"tdarr login failed: {exc}") from exc
        if not token:
            raise TransientError("tdarr login returned no token")
        self._token = token
        self._session.headers["Authorization"] = f"Bearer {token}"
        self._authed = True

    # --- requests -----------------------------------------------------------

    def _post(self, path: str, payload: dict) -> object:
        self.ensure_auth()
        url = f"{self._cfg.url}{path}"
        try:
            resp = self._session.post(url, json=payload, timeout=self._cfg.request_timeout_seconds)
            resp.raise_for_status()
            if resp.content:
                try:
                    return resp.json()
                except ValueError:
                    # A 200 is not a JSON 200. /api/v2/scan-files answers `200 text/plain` with the
                    # body "OK" regardless of the Accept header set in __init__, while /cruddb on the
                    # same server answers JSON — which is why only the scan looked like a failure.
                    #
                    # Catch ValueError, not requests.exceptions.JSONDecodeError: the latter is also a
                    # RequestException, so without this the decode error falls through to the handler
                    # below and is reported as a transport failure.
                    _log.debug("tdarr POST %s returned non-JSON: %r", path, resp.text[:80])
                    return resp.text
            return None
        except requests.RequestException as exc:
            raise TransientError(f"tdarr POST {path} failed: {exc}") from exc

    # --- operations ---------------------------------------------------------

    def list_libraries(self) -> list[dict]:
        """All library settings docs (each has `_id` and folder settings)."""
        result = self._post(
            "/api/v2/cruddb",
            {"data": {"collection": "LibrarySettingsJSONDB", "mode": "getAll"}},
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("data") or []
        return []

    def scan_files(self, library_id: str, paths: list[str], mode: str | None = None) -> None:
        """Trigger a scan of specific paths within a library, enqueuing them for the flow."""
        self._post(
            "/api/v2/scan-files",
            {
                "data": {
                    "scanConfig": {
                        "dbID": library_id,
                        "mode": mode or self.scan_mode,
                        "arrayOrPath": paths,
                    }
                }
            },
        )
        _log.info("scan-files lib=%s mode=%s n=%d", library_id, mode or self.scan_mode, len(paths))
