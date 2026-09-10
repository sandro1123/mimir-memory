# -*- coding: utf-8 -*-
"""v14.2.1-4 service-layer extraction gate alignment (second hardcoded gate)."""
import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_v1421_extract_gate import _mk_source
from mimir_v8.store import CanonicalStore
from mimir_v8.extraction import ExtractionService
from mimir_v8.schema import ValidationError


class TestServiceLayerGate(unittest.TestCase):
    def test_knowledge_doc_passes_when_gate_open(self):
        import os
        os.environ["MIMIR_EXTRACT_CATEGORIES"] = "conversation,knowledge_doc"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = CanonicalStore(Path(tmp) / "c.db")
                rid = _mk_source(store, category="knowledge_doc", connector="vault",
                                 content="note")
                # call the validator directly
                with store.connect() as c:
                    sid_db = c.execute("SELECT source_id FROM ingestion_runs WHERE run_id=?", (rid,)).fetchone()
                    sid = ExtractionService._validate_main_source(c, rid, sid_db["source_id"])
                self.assertTrue(sid)
        finally:
            os.environ.pop("MIMIR_EXTRACT_CATEGORIES", None)

    def test_conversation_always_passes_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            rid = _mk_source(store, category="conversation", connector="hermes_cdc",
                             content="chat")
            with store.connect() as c:
                sid_db = c.execute("SELECT source_id FROM ingestion_runs WHERE run_id=?", (rid,)).fetchone()
                sid = ExtractionService._validate_main_source(c, rid, sid_db["source_id"])
            self.assertTrue(sid)

    def test_unknown_category_still_rejected(self):
        import os
        os.environ["MIMIR_EXTRACT_CATEGORIES"] = "conversation"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = CanonicalStore(Path(tmp) / "c.db")
                rid = _mk_source(store, category="external_info", connector="rss",
                                 content="article")
                with self.assertRaises(ValidationError):
                    with store.connect() as c:
                        sid_db = c.execute("SELECT source_id FROM ingestion_runs WHERE run_id=?", (rid,)).fetchone()
                        ExtractionService._validate_main_source(c, rid, sid_db["source_id"])
        finally:
            os.environ.pop("MIMIR_EXTRACT_CATEGORIES", None)


if __name__ == "__main__":
    unittest.main()
