# -*- coding: utf-8 -*-
"""Issue #35 / #36: the three progressive-disclosure layers must be reachable
from an MCP client, not only from the REST API.

The MCP server already had 29 tools, but the three-layer workflow asks for a
different shape: scan cheap index rows, then hydrate only the few worth
reading, with a timeline available for the "what changed around this" case.
Without these, an MCP client's only option is ``mimir_query``, which returns
full bodies for everything.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.mcp import tool_definitions


class _StubAPI:
    """Records the client calls the MCP layer makes."""

    def __init__(self):
        self.calls = []

    def search_index(self, text, *, limit=10, **filters):
        self.calls.append(("search_index", text, limit, filters))
        return {"results": [], "recall_cursor": "cur"}

    def hydrate(self, cursor, fact_ids):
        self.calls.append(("hydrate", cursor, tuple(fact_ids)))
        return {"results": []}

    def fact_timeline(self, fact_id, *, before=5, after=5):
        self.calls.append(("fact_timeline", fact_id, before, after))
        return {"events": []}


class TestProgressiveDisclosureTools(unittest.TestCase):
    def test_all_three_layers_are_registered(self):
        names = {t["name"] for t in tool_definitions()}
        for name in ("mimir_search", "mimir_hydrate", "mimir_timeline"):
            self.assertIn(name, names)

    def test_only_the_search_tool_is_bodyless(self):
        tools = {t["name"]: t for t in tool_definitions()}
        # layer 1 must be cheap by construction: its schema carries no way to
        # ask for full content, so a client cannot accidentally blow up its
        # context by calling it.
        props = tools["mimir_search"]["inputSchema"]["properties"]
        self.assertNotIn("content", props)
        self.assertNotIn("detail", props)
        self.assertIn("include_superseded", props)

    def test_timeline_window_is_bounded_in_the_schema(self):
        tools = {t["name"]: t for t in tool_definitions()}
        props = tools["mimir_timeline"]["inputSchema"]["properties"]
        self.assertEqual(props["before"]["maximum"], 50)
        self.assertEqual(props["after"]["maximum"], 50)
        self.assertEqual(props["before"]["minimum"], 0)

    def test_dispatch_routes_to_the_right_client_method(self):
        from mimir_v8.mcp import MimirMCPServer

        api = _StubAPI()
        server = MimirMCPServer.__new__(MimirMCPServer)
        server.api = api

        server._call_tool(1, {"name": "mimir_search", "arguments": {
            "text": "zen", "limit": 3, "include_superseded": True}})
        server._call_tool(2, {"name": "mimir_hydrate", "arguments": {
            "cursor": "cur", "fact_ids": ["f1"]}})
        server._call_tool(3, {"name": "mimir_timeline", "arguments": {
            "fact_id": "f1", "before": 2, "after": 4}})

        self.assertEqual(api.calls, [
            ("search_index", "zen", 3, {"include_superseded": True}),
            ("hydrate", "cur", ("f1",)),
            ("fact_timeline", "f1", 2, 4),
        ])


if __name__ == "__main__":
    unittest.main()