from __future__ import annotations

import json
import urllib.error
import urllib.request

from zotero_core.domain.entities.gui import WindowState

DEFAULT_BRIDGE_URL = "http://127.0.0.1:23119/zotero-bridge/window-state"
DEFAULT_PING_URL = "http://127.0.0.1:23119/zotero-bridge/ping"


class ZoteroBridgeError(RuntimeError):
    pass


class ZoteroBridgeClient:
    def __init__(self, url: str = DEFAULT_BRIDGE_URL, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    def ping(self) -> dict:
        # The constant for the default URL; string surgery only for a caller-supplied one,
        # which yields the wrong endpoint if that URL lacks "/window-state".
        ping_url = (
            DEFAULT_PING_URL
            if self.url == DEFAULT_BRIDGE_URL
            else self.url.replace("/window-state", "/ping")
        )
        try:
            with urllib.request.urlopen(ping_url, timeout=self.timeout) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise ZoteroBridgeError(f"Zotero bridge is not reachable at {ping_url}: {exc}") from exc

    def get_window_state_raw(self) -> dict:
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            # An HTTPError means the bridge ANSWERED: report its status and its
            # `{"ok": false, "error": ...}` body (`bridge/bootstrap.js`), never "not
            # reachable", which would send the caller to check whether Zotero is running.
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8", "replace"))
                detail = body.get("error") or ""
            except Exception:  # noqa: BLE001 - a non-JSON body is still worth the status
                pass
            raise ZoteroBridgeError(
                f"Zotero bridge returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ZoteroBridgeError(f"Zotero bridge is not reachable at {self.url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ZoteroBridgeError("Zotero bridge returned a non-object payload")
        return payload

    def get_window_state(self) -> WindowState:
        return WindowState.from_bridge(self.get_window_state_raw())
