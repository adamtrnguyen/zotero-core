from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BBT_RPC_URL = "http://127.0.0.1:23119/better-bibtex/json-rpc"


class BetterBibTeXClient:
    def __init__(self, url: str = DEFAULT_BBT_RPC_URL, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    def citation_keys(self, item_keys: list[str]) -> dict[str, str]:
        if not item_keys:
            return {}
        result = self._call("item.citationkey", [item_keys])
        if isinstance(result, dict):
            return {str(k): str(v) for k, v in result.items() if isinstance(v, str)}
        return {}

    def search_item(self, query: str) -> dict | None:
        result = self._call("item.search", [query])
        if isinstance(result, list) and result:
            first = result[0]
            return first if isinstance(first, dict) else None
        return None

    def _call(self, method: str, params: list) -> object | None:
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload.get("result")
