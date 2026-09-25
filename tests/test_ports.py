"""The ports are only worth having if something checks that adapters satisfy them.

⚠ NOTHING DID. Every one of the twelve protocols is `@runtime_checkable`, and
`domain/ports/write_transport.py` justifies that decorator on exactly this ground -- "it
makes the fakes assertable as real implementations rather than as subclasses that happen to
work" -- while the suite contained ZERO `isinstance(adapter, Port)` assertions. The ports
reported 100% coverage because every method body is `...`, so importing the module executed
all of it.

The gap had teeth on both sides:

  * an adapter could drift out of conformance silently, because no adapter IMPORTS its port
    (by design -- that is what keeps infrastructure from depending upward), so there is no
    nominal link and nothing but a test can supply one;
  * the fakes went the other way and SUBCLASSED the concrete clients
    (`conftest.FakeLinker(LinkerClient)`), which is the subclassing trick the port was meant
    to replace. Asserting the fakes satisfy the port is what makes them a legitimate second
    implementation rather than an inheritance detail.
"""

from __future__ import annotations

import dataclasses
import email.message

import pytest

from zotero_core.application.services.session import WriteSession
from zotero_core.domain.ports.annotation_catalogue import AnnotationCatalogue
from zotero_core.domain.ports.catalogue import Catalogue
from zotero_core.domain.ports.citation_keys import CitationKeys
from zotero_core.domain.ports.collection_catalogue import CollectionCatalogue
from zotero_core.domain.ports.duplicates import DuplicateFinder
from zotero_core.domain.ports.gui_bridge import GuiBridge
from zotero_core.domain.ports.journal import Journal
from zotero_core.domain.ports.library_catalogue import LibraryCatalogue
from zotero_core.domain.ports.search_catalogue import SearchCatalogue
from zotero_core.domain.ports.write_transport import Cookjohn, Linker
from zotero_core.domain.ports.zotero_probe import ZoteroProbe
from zotero_core.infrastructure.http.bbt import BetterBibTeXClient
from zotero_core.infrastructure.http.bridge import ZoteroBridgeClient
from zotero_core.infrastructure.http.papers import WebPaperResolver
from zotero_core.infrastructure.journal import FileJournal
from zotero_core.infrastructure.probe import HttpZoteroProbe
from zotero_core.infrastructure.sqlite.annotations import ZoteroAnnotationStore
from zotero_core.infrastructure.sqlite.collections import ZoteroCollectionStore
from zotero_core.infrastructure.sqlite.duplicates import CatalogueDuplicateFinder
from zotero_core.infrastructure.sqlite.items import ZoteroItemStore
from zotero_core.infrastructure.sqlite.libraries import SqliteLibraryCatalogue
from zotero_core.infrastructure.sqlite.search import ZoteroSearchStore
from zotero_core.infrastructure.transports.cookjohn import CookjohnClient
from zotero_core.infrastructure.transports.linker import LinkerClient

from .conftest import StubProbe

# Every (port, adapter) pair the composition root actually wires. Constructing each is
# cheap and opens nothing -- the stores hold paths and ints until a query runs.
PAIRS = [
    (GuiBridge, ZoteroBridgeClient),
    (AnnotationCatalogue, ZoteroAnnotationStore),
    (CitationKeys, BetterBibTeXClient),
    (Catalogue, ZoteroItemStore),
    (CollectionCatalogue, ZoteroCollectionStore),
    (SearchCatalogue, ZoteroSearchStore),
    (LibraryCatalogue, SqliteLibraryCatalogue),
    (DuplicateFinder, CatalogueDuplicateFinder),
    (Journal, FileJournal),
    (ZoteroProbe, HttpZoteroProbe),
    (Cookjohn, CookjohnClient),
    (Linker, LinkerClient),
]


@pytest.mark.parametrize("port,adapter", PAIRS, ids=lambda x: x.__name__)
def test_every_adapter_satisfies_its_port(port, adapter):
    """Structural conformance, checked. Fails the moment a port grows a method no adapter
    has -- which is the failure `@runtime_checkable` was chosen to make detectable."""
    assert isinstance(adapter(), port)


def test_the_test_fakes_are_real_implementations_of_their_ports(linker, cookjohn):
    """The fakes subclass the concrete clients, so they could pass by inheritance alone.
    This asserts the property that actually matters: they satisfy the PORT."""
    assert isinstance(linker, Linker)
    assert isinstance(cookjohn, Cookjohn)


def test_the_stub_probe_is_a_real_implementation_too():
    """`StubProbe` is hand-written rather than subclassed, so it is the one adapter whose
    conformance rests entirely on shape."""
    assert isinstance(StubProbe(running=True), ZoteroProbe)
    assert isinstance(StubProbe(running=False), ZoteroProbe)


def test_a_write_session_cannot_be_mutated_mid_verb():
    """`WriteSession` is `frozen=True` because a session DESCRIBES one operation's
    collaborators; it is not a place to stash state mid-verb. Nothing asserted it, so
    flipping `frozen` to False went undetected."""
    session = WriteSession(
        linker=LinkerClient(),
        cookjohn=CookjohnClient(),
        store=ZoteroItemStore(),
        collections=ZoteroCollectionStore(),
        journal=FileJournal(),
        probe=StubProbe(),
        duplicates=CatalogueDuplicateFinder(),
        papers=WebPaperResolver(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        session.linker = CookjohnClient()  # ty: ignore[invalid-assignment]


# --------------------------------------------------------------------------
# HttpZoteroProbe -- the adapter with no tests at all
# --------------------------------------------------------------------------
# Its whole body could be replaced with `return False` and the suite stayed green.
# The behaviour is subtle enough to be worth pinning: an HTTP ERROR counts as RUNNING.


def test_any_answer_means_zotero_is_running(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse())
    assert HttpZoteroProbe().is_running() is True


def test_an_http_error_still_means_zotero_is_running(monkeypatch):
    """THE SUBTLE ONE. Zotero's server 404s an unknown path, so treating an HTTPError as
    "not running" would make the probe report the exact opposite of the truth -- and that
    verdict decides whether a caller is told to start Zotero or to install a plugin."""
    import io
    import urllib.error

    def _raise(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:23119/", 404, "NF", email.message.Message(), io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    assert HttpZoteroProbe().is_running() is True


def test_a_refused_connection_means_zotero_is_not_running(monkeypatch):
    def _raise(*_a, **_k):
        raise OSError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    assert HttpZoteroProbe().is_running() is False


def test_the_probe_reports_the_url_it_probed():
    """`require_zotero` puts this in the refusal detail, so a caller can see WHAT was
    asked. A probe that could not say would make that message unfalsifiable."""
    assert HttpZoteroProbe("http://example.invalid/x").url == "http://example.invalid/x"


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


# --------------------------------------------------------------------------
# rendering -- one serialiser, because the three copies disagreed
# --------------------------------------------------------------------------


class _Unserialisable:
    """A value `json.dumps` cannot handle and `to_jsonable` does not model."""

    def __repr__(self) -> str:
        return "<unserialisable>"


def test_an_unserialisable_value_survives_every_surface():
    """THE ASYMMETRY THIS CLOSES.

    `json.dumps` was called at three sites and only ONE passed `default=str`:

        write_mcp._render   default=str        survived
        cli.print_json      (none)             raised
        read_mcp inline     (none)             raised

    So the same payload killed the read adapter and was survived by the write adapter.
    A `Path` or a `datetime` in a `WriteBlocked.detail` is enough to hit it -- `detail` is
    whatever the gate put there, which is precisely what the domain does not model.

    All three go through `render_json` now. This asserts the property directly rather than
    asserting that they share a function, because sharing is the mechanism and surviving is
    the point.
    """
    from zotero_core.interfaces.rendering import render_json

    payload = {"ok": False, "detail": {"odd": _Unserialisable()}}
    rendered = render_json(payload)
    assert "unserialisable" in rendered


def test_the_write_adapter_and_the_read_adapter_render_identically():
    """Both adapters and the CLI must degrade the SAME way, or a caller debugging one is
    reading a different failure mode from the other."""
    from zotero_core.interfaces.rendering import render_json
    from zotero_core.interfaces.write_mcp import _render

    payload = {"detail": {"odd": _Unserialisable()}}
    assert _render(payload) == render_json(payload)


def test_the_cli_compact_form_is_still_compact():
    """`print_json` without `--pretty` emits `indent=None`; routing it through the shared
    renderer must not silently start pretty-printing every CLI result."""
    from zotero_core.interfaces.rendering import render_json

    assert "\n" not in render_json({"a": 1, "b": 2}, indent=None)
    assert "\n" in render_json({"a": 1, "b": 2})
