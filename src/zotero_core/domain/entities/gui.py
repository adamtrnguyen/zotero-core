"""The zotero-bridge payload: what the Zotero window is showing right now.

Every type here is built by `from_bridge` from the JSON at
`:23119/zotero-bridge/window-state`, and every one accepts BOTH camelCase and snake_case
because the plugin has emitted both. `WindowState.raw` keeps the untransformed payload.

That tolerance is why these sit apart from `models.py`: it is anti-corruption for one HTTP
contract, not a description of Zotero. They are live GUI state — an item selected here has
no more claim to be "the" item than any other row in the database.

`ReaderContext` is the one join across both files: a bridge `ReaderState` plus a BBT citekey
plus SQL `Annotation`s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zotero_core.domain.entities.models import Annotation


@dataclass(frozen=True)
class ZoteroCollection:
    key: str
    name: str
    id: int | None = None

    @classmethod
    def from_bridge(cls, data: dict[str, Any] | None) -> ZoteroCollection | None:
        if not data:
            return None
        return cls(
            key=str(data.get("key") or ""),
            name=str(data.get("name") or ""),
            id=data.get("id"),
        )


@dataclass(frozen=True)
class ZoteroItem:
    key: str
    item_type: str
    title: str
    is_attachment: bool
    parent_key: str | None = None

    @classmethod
    def from_bridge(cls, data: dict[str, Any]) -> ZoteroItem:
        return cls(
            key=str(data.get("key") or ""),
            item_type=str(data.get("itemType") or data.get("item_type") or ""),
            title=str(data.get("title") or ""),
            is_attachment=bool(data.get("isAttachment") or data.get("is_attachment")),
            parent_key=data.get("parentKey") or data.get("parent_key"),
        )


@dataclass(frozen=True)
class TabState:
    type: str
    title: str
    item_id: int | None = None
    selected: bool = False

    @classmethod
    def from_bridge(cls, data: dict[str, Any]) -> TabState:
        return cls(
            type=str(data.get("type") or ""),
            title=str(data.get("title") or ""),
            item_id=data.get("itemID") or data.get("item_id"),
            selected=bool(data.get("selected")),
        )


@dataclass(frozen=True)
class ReaderPosition:
    page_index: int | None = None
    page_number: int | None = None
    scroll_y_percent: float | None = None
    cfi: str | None = None

    @classmethod
    def from_bridge(cls, data: dict[str, Any] | None) -> ReaderPosition | None:
        if not data:
            return None
        return cls(
            page_index=_pick(data, "pageIndex", "page_index"),
            page_number=_pick(data, "pageNumber", "page_number"),
            scroll_y_percent=_pick(data, "scrollYPercent", "scroll_y_percent"),
            cfi=data.get("cfi"),
        )


@dataclass(frozen=True)
class ReaderState:
    tab_id: str
    is_active_tab: bool
    attachment_key: str | None
    content_type: str | None
    parent_key: str | None
    title: str | None
    position: ReaderPosition | None = None

    @classmethod
    def from_bridge(cls, data: dict[str, Any]) -> ReaderState:
        return cls(
            tab_id=str(data.get("tabID") or data.get("tab_id") or ""),
            is_active_tab=bool(data.get("isActiveTab") or data.get("is_active_tab")),
            attachment_key=data.get("attachmentKey") or data.get("attachment_key"),
            content_type=data.get("contentType") or data.get("content_type"),
            parent_key=data.get("parentKey") or data.get("parent_key"),
            title=data.get("title"),
            position=ReaderPosition.from_bridge(data.get("position")),
        )


@dataclass(frozen=True)
class WindowState:
    ok: bool
    zotero_version: str
    collected_at: str
    selected_collection: ZoteroCollection | None
    selected_items: list[ZoteroItem]
    selected_tab_id: str | None
    selected_tab_type: str | None
    tabs: list[TabState]
    readers: list[ReaderState]
    errors: dict[str, str]
    raw: dict[str, Any]

    @classmethod
    def from_bridge(cls, data: dict[str, Any]) -> WindowState:
        sections = data.get("sections") or {}
        tabs_section = sections.get("tabs") or {}
        return cls(
            ok=bool(data.get("ok")),
            zotero_version=str(data.get("zoteroVersion") or data.get("zotero_version") or ""),
            collected_at=str(data.get("collectedAt") or data.get("collected_at") or ""),
            selected_collection=ZoteroCollection.from_bridge(sections.get("selectedCollection")),
            selected_items=[
                ZoteroItem.from_bridge(item) for item in sections.get("selectedItems", [])
            ],
            selected_tab_id=tabs_section.get("selectedID") or tabs_section.get("selected_id"),
            selected_tab_type=tabs_section.get("selectedType") or tabs_section.get("selected_type"),
            tabs=[TabState.from_bridge(tab) for tab in tabs_section.get("all", [])],
            readers=[ReaderState.from_bridge(reader) for reader in sections.get("readers", [])],
            errors={str(k): str(v) for k, v in (data.get("errors") or {}).items()},
            raw=data,
        )


@dataclass(frozen=True)
class ReaderContext:
    reader: ReaderState
    citekey: str = ""
    annotations: list[Annotation] | None = None


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None
