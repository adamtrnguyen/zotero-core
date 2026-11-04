"""HTTP transport to the zotero-linker plugin — the sanctioned mutator.

This module is deliberately dumb. It knows how to reach the plugin and how to
turn its replies into exceptions; it knows nothing about what a valid write is.
Every precondition lives in `application/services/verbs.py`, so there is one place to read to find
out what is enforced.

WHY THE PLUGIN AND NOT SOMETHING ELSE
-------------------------------------
The four channels into local Zotero, from docs/DESIGN.md:

  * Local REST API `:23119/api` — GET-only, "Write access is not yet supported"
  * Connector API `:23119/connector/*` — create-only; cannot modify existing items
  * direct `zotero.sqlite` — forbidden read-write by ZoteroSuite's own rules
  * in-process plugin JS — the only channel with unrestricted local writes

So the plugin is not a preference, it is the only door. `linker/bootstrap.js` registers
`link-attachment`, `trash-items`, `restore-items` and `create-annotation` on it; this
module is their client.

NOTHING CHECKS THE PLUGIN VERSION. `ping()` verifies only `plugin == "zotero-linker"`, so
an install too old for an endpoint passes liveness and then 404s at the POST, reported as
LINKER_REFUSED ("the plugin considered this invalid") rather than "your plugin is stale",
because the two are indistinguishable from here. The `version` in `info` is carried into
every result and compared against nothing.

READING ERRORS OUT OF urllib
----------------------------
The plugin signals refusal with a non-2xx status AND a JSON body
(`{"ok": false, "error": ...}` — see `fail()` in bootstrap.js). urllib raises
`HTTPError` on 4xx/5xx, and an HTTPError IS a response object, so the body has to
be read off the exception or the plugin's actual reason is discarded and replaced
with a bare "HTTP Error 404: Not Found".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from zotero_core.domain.errors import Reason, WriteBlocked

DEFAULT_LINKER_URL = "http://127.0.0.1:23119/zotero-linker"


class LinkerClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 30.0):
        # Resolved at CALL time, not import time. `base_url=DEFAULT_LINKER_URL` as a
        # default ARGUMENT binds the module constant when the `def` executes, so
        # redirecting the constant afterwards -- which is exactly what the test
        # fixture does to keep a forgotten injection off the live Zotero -- would
        # silently do nothing. calibre-core documents the same trap for its
        # `calibredb_path()`: resolve on use, or the seam is decorative.
        self.base_url = (base_url or DEFAULT_LINKER_URL).rstrip("/")
        self.timeout = timeout

    def ping(self) -> dict:
        """Liveness. Raises rather than returning False, so a caller cannot ignore it.

        Distinguishes three failures that look the same from a distance and need
        different fixes: Zotero is not running, something else is answering on that
        port, and the plugin is not installed in the running Zotero.
        """
        try:
            with urllib.request.urlopen(f"{self.base_url}/ping", timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            # The port answered but this path did not: Zotero is up without the
            # plugin. bootstrap.js is not loaded, so no write endpoint exists either.
            raise WriteBlocked(
                Reason.LINKER_NOT_INSTALLED,
                f"Zotero is running but {self.base_url}/ping returned HTTP {exc.code} — "
                "the zotero-linker plugin is not installed or not started",
                {"url": f"{self.base_url}/ping", "http_status": exc.code},
            ) from exc
        except OSError as exc:
            # Connection refused / timeout: nothing is listening on :23119.
            raise WriteBlocked(
                Reason.ZOTERO_NOT_RUNNING,
                f"no answer from {self.base_url}/ping — Zotero must be RUNNING for any "
                f"write to be possible ({exc})",
                {"url": f"{self.base_url}/ping", "error": str(exc)},
            ) from exc

        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise WriteBlocked(
                Reason.NOT_THE_LINKER,
                f"{self.base_url}/ping did not return JSON",
                {"body": raw[:200]},
            ) from exc
        if info.get("plugin") != "zotero-linker":
            raise WriteBlocked(
                Reason.NOT_THE_LINKER,
                f"something other than zotero-linker is answering at {self.base_url}",
                {"reply": info},
            )
        return info

    def post(self, path: str, payload: dict) -> dict:
        """POST JSON to one endpoint, returning the decoded reply.

        Raises on transport failure and on the plugin's own refusal. Does NOT
        inspect the reply's semantics -- `{"ok": true, "trashed": 3, "missing": [..]}`
        is a successful POST that may still be a partial apply, and judging that is
        the gate's job, not the transport's.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode()
            except Exception:  # noqa: BLE001 - the status still has to be reported
                pass
            detail = {"url": url, "http_status": exc.code, "body": body[:400]}
            try:
                detail["error"] = json.loads(body).get("error")
            except ValueError:
                pass
            raise WriteBlocked(
                Reason.LINKER_REFUSED,
                f"{path} returned HTTP {exc.code}: {detail.get('error') or body[:200]}",
                detail,
            ) from exc
        except OSError as exc:
            raise WriteBlocked(
                Reason.ZOTERO_NOT_RUNNING,
                f"{path} could not be reached — Zotero stopped answering mid-write ({exc})",
                {"url": url, "error": str(exc)},
            ) from exc

        try:
            reply = json.loads(raw)
        except ValueError as exc:
            raise WriteBlocked(
                Reason.NOT_THE_LINKER,
                f"{path} did not return JSON",
                {"url": url, "body": raw[:200]},
            ) from exc
        if not reply.get("ok"):
            raise WriteBlocked(
                Reason.LINKER_REFUSED,
                f"{path} refused: {reply.get('error')}",
                {"url": url, "reply": reply},
            )
        return reply
