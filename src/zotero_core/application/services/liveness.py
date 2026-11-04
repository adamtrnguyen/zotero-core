"""The liveness gate. One probe, three distinguishable failures.

THE PRECONDITION THAT INVERTS
-----------------------------
calibre-core's first gate is "the Calibre GUI must be CLOSED" -- `calibredb`
corrupts state if it writes underneath a running GUI. Zotero is the exact opposite:
every write channel is code executing INSIDE the application, so a closed Zotero is
not a safe state, it is no channel at all.

WHY THREE FAILURES AND NOT ONE BOOLEAN
--------------------------------------
The surface spans two plugins on two ports, and they fail independently. Collapsing
that into "Zotero is not available" would send someone to start an application that
is already running. So the probe walks a hierarchy:

    :23119/            -- Zotero's own built-in server. Present whenever Zotero runs,
                          with no plugin involved, which makes it the authoritative
                          "is the application up" signal.
    :23119/zotero-linker/ping   -- the linker plugin (trash, restore, linked files)
    :23121/mcp                  -- the cookjohn plugin (items, metadata, collections)

Connection refused at :23119 means the app is down (`zotero_not_running`). :23119
answering while a plugin path does not means the app is up and that ONE plugin is
missing (`linker_not_installed` / `cookjohn_not_installed`), which is a different
job to fix.

Each operation declares the transports it actually needs, so a metadata write is
not blocked by a missing linker and a trash is not blocked by a missing cookjohn.
"""

from __future__ import annotations

from zotero_core.domain.errors import Reason, WriteBlocked
from zotero_core.domain.ports.write_transport import Cookjohn, Linker
from zotero_core.domain.ports.zotero_probe import ZoteroProbe

# ⚠ `zotero_is_running` AND `ZOTERO_SERVER_URL` LEFT THIS MODULE. The function called
# `urllib.request.urlopen` directly, which put network I/O in the application layer and
# left the test suite no way in except `monkeypatch.setattr(".. .zotero_is_running", ...)`
# at three sites. Both now live in `infrastructure/probe.py` behind `ZoteroProbe`.


def require_zotero(
    *,
    needs: tuple[str, ...] = ("linker",),
    linker: Linker,
    cookjohn: Cookjohn | None = None,
    probe: ZoteroProbe,
) -> dict:
    """Refuse unless Zotero is running with the plugins this operation needs.

    Returns {"zotero": ..., "linker": ..., "cookjohn": ...} for whatever was probed,
    so the result of a write can record which versions performed it.
    """
    info: dict = {}
    for transport in needs:
        try:
            if transport == "linker":
                info["linker"] = linker.ping()
            elif transport == "cookjohn":
                if cookjohn is None:
                    raise ValueError("this operation needs cookjohn, which was not provided")
                info["cookjohn"] = cookjohn.ping()
            else:
                raise ValueError(f"unknown transport {transport!r}")
        except WriteBlocked as exc:
            # A plugin did not answer. Which of the two failures it is depends on
            # whether the APPLICATION is up, so that is checked only now -- on the
            # failure path, where the extra request costs nothing anybody waits for.
            if (
                exc.code
                in (
                    Reason.ZOTERO_NOT_RUNNING,
                    Reason.COOKJOHN_NOT_INSTALLED,
                )
                and not probe.is_running()
            ):
                raise WriteBlocked(
                    Reason.ZOTERO_NOT_RUNNING,
                    "Zotero is not running — every write channel is code executing "
                    "inside the application, so there is no way in while it is closed",
                    {"needed": list(needs), "probe": probe.url},
                ) from exc
            raise
    return info
