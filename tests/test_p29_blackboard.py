"""Mímir v13.0 — 多 Agent 共享工作黑板 (spec 阶段三任务1).

跨 Agent 秒级共享瞬态任务上下文（Task Scratchpad）：
- 轻量 SQLite 独立文件（blackboard.db），不占 memory_events 主链——
  黑板是瞬态层，任务结束「沉淀为长期事实」时才走 create_fact 事件流。
- 多 Agent 读写同一 board 的条目（append/post/list/read 标记），
  最后读取时间可观测（支持「谁在什么时候看到了什么」的协同语义）。
- 任务结束两个出口：distill（自动摘要沉淀为 pattern 事实）或
  destroy（安全销毁——条目与 board 全清，可审计销毁发生本身）。
- ACL：board 参与者集（participants）外的 agent 不可读写。

TDD RED → GREEN.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.blackboard import (
    BlackboardError,
    BlackboardService,
    CreateBoard,
    PostEntry,
)
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _fact_ids(store: CanonicalStore, fact_type: str) -> set[str]:
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT fact_id FROM facts WHERE fact_type=?", (fact_type,)
        ).fetchall()
    return {row["fact_id"] for row in rows}


class BlackboardFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.service = BlackboardService(self.store, root / "blackboard.db")

    def create_board(self, **kw):
        defaults = dict(
            board_id="bb-incident-42",
            title="K8s node drift incident",
            participants=("mentor", "jarvis", "quantmaster"),
        )
        defaults.update(kw)
        return self.service.create_board(CreateBoard(**defaults))

    def post(self, board_id, content, author="mentor", **kw):
        return self.service.post_entry(
            PostEntry(board_id=board_id, content=content, author=author, **kw),
            actor_principal=author,
        )


class TestBlackboardLifecycle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = BlackboardFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_board_and_list_entries(self):
        board = self.fx.create_board()
        self.fx.post(board["board_id"], "03:00 节点 cpu95 出现漂移")
        self.fx.post(board["board_id"], "确认 cgroup 泄漏", author="jarvis")
        entries = self.fx.service.list_entries(board["board_id"], actor_principal="jarvis")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["content"], "03:00 节点 cpu95 出现漂移")
        self.assertEqual(entries[1]["author"], "jarvis")

    def test_create_board_is_idempotent_on_board_id(self):
        self.fx.create_board()
        with self.assertRaises(BlackboardError):
            self.fx.create_board()  # same board_id -> conflict

    def test_post_rejects_non_participant(self):
        board = self.fx.create_board()
        with self.assertRaises(BlackboardError):
            self.fx.post(board["board_id"], "旁路者", author="heimdallr")

    def test_list_rejects_non_participant(self):
        board = self.fx.create_board()
        with self.assertRaises(BlackboardError):
            self.fx.service.list_entries(board["board_id"], actor_principal="heimdallr")

    def test_read_marks_last_seen(self):
        board = self.fx.create_board()
        self.fx.post(board["board_id"], "entry one")
        self.fx.post(board["board_id"], "entry two", author="jarvis")
        before = self.fx.service.read_since(
            board["board_id"], actor_principal="jarvis", after_seq=0
        )
        self.assertEqual(len(before), 2)
        # jarvis 读后，mentor 增量拉取只看到 jarvis 之后的新条目
        self.fx.post(board["board_id"], "entry three", author="mentor")
        delta = self.fx.service.read_since(
            board["board_id"], actor_principal="jarvis", after_seq=2
        )
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["content"], "entry three")

    def test_distill_materializes_pattern_fact_via_event_stream(self):
        board = self.fx.create_board()
        self.fx.post(board["board_id"], "排障：kubectl drain cpu95")
        self.fx.post(board["board_id"], "根因 cgroup 泄漏，重启 kubelet", author="jarvis")
        result = self.fx.service.distill(
            board["board_id"], actor_principal="mentor",
            summary="节点漂移标准排障：drain→查 cgroup→重启 kubelet",
        )
        patterns = _fact_ids(self.fx.store, "pattern")
        self.assertEqual(len(patterns), 1)
        fact = self.fx.store.get_fact(next(iter(patterns)))
        self.assertIn("节点漂移标准排障", fact["content"])
        self.assertEqual(fact["owner_principal"], "mentor")
        # board 已 distill 终结，不可再写
        with self.assertRaises(BlackboardError):
            self.fx.post(board["board_id"], "late entry")
        self.assertTrue(result["fact_id"])

    def test_destroy_wipes_entries_safely(self):
        board = self.fx.create_board()
        self.fx.post(board["board_id"], "敏感临时数据")
        result = self.fx.service.destroy(
            board["board_id"], actor_principal="mentor", reason="任务终止"
        )
        self.assertEqual(result["destroyed_entries"], 1)
        self.assertEqual(self.fx.store.counts()["facts"], 0)  # 无沉淀
        with self.assertRaises(BlackboardError):
            self.fx.service.list_entries(board["board_id"], actor_principal="mentor")

    def test_destroy_requires_reason(self):
        board = self.fx.create_board()
        with self.assertRaises(BlackboardError):
            self.fx.service.destroy(board["board_id"], actor_principal="mentor", reason="")

    def test_post_to_unknown_board_fails_closed(self):
        with self.assertRaises(BlackboardError):
            self.fx.post("bb-ghost", "hello")


class TestBlackboardPersistence(unittest.TestCase):
    """黑板跨服务实例持久（SQLite 文件，非进程内存）。"""

    def test_reopen_service_sees_earlier_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fx = BlackboardFixture(root)
            board = fx.create_board()
            fx.post(board["board_id"], "persisted entry")
            reopened = BlackboardService(fx.store, root / "blackboard.db")
            entries = reopened.list_entries(board["board_id"], actor_principal="mentor")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["content"], "persisted entry")


class TestBlackboardAPI(unittest.TestCase):
    """/v13/blackboard/* — REST 面钉死 scope 与 actor 不可伪造。"""

    def setUp(self):
        import hashlib
        import json

        from fastapi.testclient import TestClient

        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.query import QueryKernel

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canonical.db")
        self.service = BlackboardService(self.store, root / "blackboard.db")
        token_path = root / "tokens.json"
        tokens = {"mentor": "tok-mentor", "heimdallr": "tok-other"}
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": principal,
                        "token_sha256": hashlib.sha256(
                            token.encode("utf-8")
                        ).hexdigest(),
                        "scopes": ["read", "write", "ingest"],
                        "roles": [],
                        "admin": False,
                    }
                    for principal, token in tokens.items()
                ]
            }),
            encoding="utf-8",
        )
        self.client = TestClient(
            create_app(
                ServiceContext(
                    store=self.store,
                    token_store=TokenStore(token_path),
                    query=QueryKernel(self.store),
                    blackboard=self.service,
                )
            ),
            raise_server_exceptions=False,
        )
        self.headers = {"Authorization": "Bearer tok-mentor"}
        self.other_headers = {"Authorization": "Bearer tok-other"}

    def tearDown(self):
        self._tmp.cleanup()

    def _create_board(self, **overrides):
        body = {
            "board_id": "bb-api-1",
            "title": "API board",
            "participants": ["mentor"],
        }
        body.update(overrides)
        return self.client.post(
            "/v13/blackboard/boards", json=body, headers=self.headers
        )

    def test_board_crud_over_rest(self):
        created = self._create_board()
        self.assertEqual(created.status_code, 201, created.text)
        posted = self.client.post(
            "/v13/blackboard/boards/bb-api-1/entries",
            json={"content": "rest entry"},
            headers=self.headers,
        )
        self.assertEqual(posted.status_code, 201, posted.text)
        listed = self.client.get(
            "/v13/blackboard/boards/bb-api-1/entries", headers=self.headers
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["entries"]), 1)

    def test_author_cannot_be_spoofed_via_body(self):
        self._create_board(participants=["mentor", "heimdallr"])
        # body 声明 author=heimdallr，但 token 是 mentor → 必须以 mentor 落笔
        posted = self.client.post(
            "/v13/blackboard/boards/bb-api-1/entries",
            json={"content": "spoof attempt", "author": "heimdallr"},
            headers=self.headers,
        )
        self.assertEqual(posted.status_code, 201, posted.text)
        self.assertEqual(posted.json()["author"], "mentor")

    def test_non_participant_rejected_over_rest(self):
        self._create_board()
        denied = self.client.post(
            "/v13/blackboard/boards/bb-api-1/entries",
            json={"content": "outsider"},
            headers=self.other_headers,
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "policy_rejected")

    def test_distill_and_destroy_over_rest(self):
        self._create_board()
        self.client.post(
            "/v13/blackboard/boards/bb-api-1/entries",
            json={"content": "work note"},
            headers=self.headers,
        )
        distilled = self.client.post(
            "/v13/blackboard/boards/bb-api-1/distill",
            json={"summary": "REST 沉淀摘要"},
            headers=self.headers,
        )
        self.assertEqual(distilled.status_code, 200, distilled.text)
        self.assertIn("fact_id", distilled.json())
        late = self.client.post(
            "/v13/blackboard/boards/bb-api-1/entries",
            json={"content": "too late"},
            headers=self.headers,
        )
        self.assertEqual(late.status_code, 403, late.text)

    def test_requires_authentication(self):
        r = self.client.post(
            "/v13/blackboard/boards",
            json={"board_id": "x", "title": "y", "participants": ["mentor"]},
        )
        self.assertEqual(r.status_code, 401, r.text)


if __name__ == "__main__":
    unittest.main()
