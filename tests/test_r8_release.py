import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from mimir_v8.runtime import RuntimeConfigurationError, build_runtime, normalize_knowledge_layers
from mimir_v8.schema import MIMIR_VERSION, SCHEMA_VERSION
from mimir_v8.server import build_app, parse_knowledge_layers
from mimir_v8.store import CanonicalStore
from mimir_v8.vector_projector import VectorProjectionError, validate_vector_collection_name


class TestKnowledgeLayerConfiguration(unittest.TestCase):
    def test_normalizes_order_case_and_duplicates(self):
        self.assertEqual(
            normalize_knowledge_layers(("WIKI", "memory", "wiki", "learning")),
            ("memory", "learning", "wiki"),
        )
        self.assertEqual(parse_knowledge_layers("wiki,memory"), ("memory", "wiki"))

    def test_empty_or_unknown_layers_fail_closed(self):
        for value in ((), ("",), ("memory", "unknown")):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeConfigurationError):
                    normalize_knowledge_layers(value)
        for raw in ("", "  ", "memory,unknown"):
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeConfigurationError):
                    parse_knowledge_layers(raw)

    def test_build_runtime_enforces_layers_without_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            CanonicalStore(root / "canonical.db")
            token_file = root / "tokens.json"
            token_file.write_text("[]", encoding="utf-8")
            with self.assertRaises(RuntimeConfigurationError):
                build_runtime(
                    root,
                    token_file,
                    vector_enabled=False,
                    start_supervisor=False,
                    enabled_knowledge_layers=("memory", "invalid"),
                )

    def test_server_forwards_explicit_layers(self):
        with patch("mimir_v8.server.build_runtime") as runtime:
            runtime.return_value = (object(), object())
            app = build_app(
                "/tmp/data",
                "/tmp/tokens.json",
                vector_enabled=False,
                enabled_knowledge_layers=("memory", "learning"),
            )
        self.assertIsNotNone(app)
        self.assertEqual(
            runtime.call_args.kwargs["enabled_knowledge_layers"],
            ("memory", "learning"),
        )

    def test_vector_collection_names_are_versioned_and_fail_closed(self):
        expected = {
            "mimir_v8_shadow_legacy": "staging",
            "mimir_v8_prod_legacy": "production",
            "mimir_v9_shadow_rc3": "staging",
            "mimir_v9_prod_20260805": "production",
        }
        for name, projection in expected.items():
            with self.subTest(name=name):
                self.assertEqual(validate_vector_collection_name(name), projection)
        for name in ("mimir_facts", "mimir_v9_", "mimir_v11_prod_x", ""):
            with self.subTest(name=name):
                with self.assertRaises(VectorProjectionError):
                    validate_vector_collection_name(name)

    def test_release_identity(self):
        # MIMIR_VERSION 硬编码是**有意的发布闸门**：发版时必须回来改这一
        # 行，从而强制审视发布身份（五件套对拍）。保留。
        self.assertEqual(MIMIR_VERSION, "1.0.0")
        # 原 `SCHEMA_VERSION == 20` 已删：schema 提版是开发期常规动作，
        # 硬编码在此产生的是必假红而非闸门。真守卫由 test_p0o 承担且更强
        # （断言运行时 schema 已在迁移链目标集内，提版忘配迁移段即红）。

    def test_schema_version_is_in_migration_chain(self):
        """发布身份层复核：运行时 schema 必须是迁移链登记过的目标版本。"""
        from mimir_v8.migration import MIGRATABLE_TARGET_VERSIONS

        self.assertIn(SCHEMA_VERSION, MIGRATABLE_TARGET_VERSIONS)

    def test_pypi_packaging_metadata(self):
        root = Path(__file__).resolve().parent.parent
        pyproject = root / "pyproject.toml"
        self.assertTrue(pyproject.is_file())
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["name"], "mimir-v8")
        self.assertEqual(project["version"], MIMIR_VERSION)
        scripts = data["project"]["scripts"]
        for entry in ("mimir-server", "mimir-worker", "mimir-migrate", "mimir-cli"):
            self.assertIn(entry, scripts)

    def test_dockerfile_is_self_contained(self):
        root = Path(__file__).resolve().parent.parent
        dockerfile = root / "Dockerfile"
        self.assertTrue(dockerfile.is_file())
        text = dockerfile.read_text(encoding="utf-8")
        self.assertIn("COPY mimir_v8 ./mimir_v8", text)
        self.assertIn("ENTRYPOINT", text)
        # every COPY source must exist relative to the release root
        for line in text.splitlines():
            if line.lstrip().startswith("COPY") and "from=" not in line.lower() \
                    and "builder" not in line.lower():
                parts = line.split()
                source = parts[1]
                self.assertTrue(
                    (root / source).exists(),
                    f"Dockerfile COPY source missing: {source}",
                )

    def test_obsidian_wikilink_module_registered(self):
        from mimir_v8 import wikilink
        self.assertTrue(callable(wikilink.fact_note))
        self.assertTrue(callable(wikilink.related_links))


if __name__ == "__main__":
    unittest.main()
