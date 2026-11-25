# -*- coding: utf-8 -*-
"""1.9.1 人审积压工具：review-backlog list / apply。

list 只读；apply 默认演练，--execute 才写；每条决定走 CandidateService 的
审计路径（review_candidate，approve 再 commit_approved），单条失败不影响其余，
有失败即非零退出；预览遵守 sensitivity 与 redact_text。
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mimir_v8 import review_backlog as rb
from mimir_v8.store import CanonicalStore, utc_now
from mimir_v8.worker import build_parser

BRIDGE = '<bridge_context>\n{"chatId":"oc_x"}\n</bridge_context>\n<user_input>{"text":"hi"}</user_input>'


def _insert(store, cid, *, content=None, owner="mentor", sensitivity="internal",
            reason="高风险(high)", created_at=None, status="human_review"):
    now = utc_now()
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_facts(candidate_id,content,summary,proposed_owner_principal,"
            "proposed_domain,proposed_fact_type,proposed_visibility,proposed_sensitivity,"
            "proposed_egress_policy,proposed_by,uncertainty_json,status,review_reason,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, content or f"用户偏好：每次汇报先给结论 {cid}", f"summary {cid}", owner,
             "system", "pattern", "owner_only", sensitivity, "local_only", "service:test",
             "[]", status, reason, created_at or now, now),
        )


def _status(store, cid):
    with store.transaction() as c:
        return c.execute("SELECT status FROM candidate_facts WHERE candidate_id=?",
                         (cid,)).fetchone()["status"]


def _run(store, argv):
    args = build_parser().parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = rb.run_review_backlog(store, args)
    return code, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = CanonicalStore(Path(tmp.name) / "canonical.db")


class TestBridgeNoise(unittest.TestCase):
    def test_prefix_is_noise(self):
        self.assertTrue(rb.is_bridge_noise(BRIDGE))

    def test_dominated_by_blocks_is_noise(self):
        text = "ok " + "<system-reminder>" + "x" * 200 + "</system-reminder>"
        self.assertTrue(rb.is_bridge_noise(text))

    def test_plain_text_is_not_noise(self):
        self.assertFalse(rb.is_bridge_noise("用户偏好：先给结论"))
        self.assertFalse(rb.is_bridge_noise(""))

    def test_older_than(self):
        self.assertEqual(rb.parse_older_than("3d"), 72.0)
        self.assertEqual(rb.parse_older_than("12h"), 12.0)
        self.assertIsNone(rb.parse_older_than(""))
        with self.assertRaises(ValueError):
            rb.parse_older_than("3w")


class TestList(Base):
    def setUp(self):
        super().setUp()
        _insert(self.store, "plain")
        _insert(self.store, "noise", content=BRIDGE)
        _insert(self.store, "odin1", owner="odin", reason="DuplicateBlocked 判重拦截")
        _insert(self.store, "old", created_at="2026-10-01T00:00:00+00:00")
        _insert(self.store, "secret", sensitivity="restricted", content="密码是 hunter2")
        _insert(self.store, "done", status="approved")

    def _list(self, *extra):
        code, out = _run(self.store, ["review-backlog", "list", "--json", *extra])
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_only_human_review(self):
        data = self._list()
        self.assertEqual(data["total"], 5)
        self.assertNotIn("done", {i["candidate_id"] for i in data["items"]})

    def test_filters(self):
        self.assertEqual({i["candidate_id"] for i in self._list("--owner", "odin")["items"]}, {"odin1"})
        self.assertEqual({i["candidate_id"] for i in self._list("--bridge-noise")["items"]}, {"noise"})
        self.assertEqual({i["candidate_id"] for i in self._list("--older-than", "7d")["items"]}, {"old"})
        self.assertEqual({i["candidate_id"] for i in self._list("--reason-code", "duplicate_gate")["items"]},
                         {"odin1"})

    def test_reason_falls_back_to_governance_decision(self):
        _insert(self.store, "gov", reason=None)
        with self.store.transaction() as c:
            c.execute("INSERT INTO governance_decisions(decision_id,candidate_id,decision,reason,"
                      "previous_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)",
                      ("d1", "gov", "human_review", "LLM 评估不可用，需人工确认",
                       "review_required", "human_review", utc_now()))
        item = next(i for i in self._list()["items"] if i["candidate_id"] == "gov")
        self.assertEqual(item["reason_code"], "llm_unavailable")

    def test_summary(self):
        data = self._list()
        self.assertEqual(data["bridge_noise"], 1)
        self.assertEqual(data["by_reason"]["high_risk"], 4)

    def test_restricted_preview_is_redacted(self):
        item = next(i for i in self._list()["items"] if i["candidate_id"] == "secret")
        self.assertNotIn("hunter2", item["preview"])
        self.assertIn("redacted", item["preview"])

    def test_list_writes_nothing(self):
        self._list()
        self.assertEqual(_status(self.store, "plain"), "human_review")


def _registry(dirpath):
    """Token registry with a reviewer (thor + review) and a non-reviewer (odin)."""
    from mimir_v8.auth import mint_principal
    reg = Path(dirpath) / "api_tokens.json"
    reg.write_text(json.dumps({"version": 1, "principals": []}), encoding="utf-8")
    tokens = {}
    for pid, scopes in (("thor", ["ingest", "read", "write", "review"]),
                        ("odin", ["ingest", "read", "write"])):
        tok, _ = mint_principal(reg, principal_id=pid, scopes=scopes)
        tf = Path(dirpath) / f"{pid}.token"
        tf.write_text(tok, encoding="utf-8")
        tokens[pid] = tf
    return reg, tokens


class TestApply(Base):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry, self.tokens = _registry(tmp.name)
        _insert(self.store, "a")
        _insert(self.store, "b")
        _insert(self.store, "noise", content=BRIDGE)

    def _apply(self, *extra, who="thor"):
        argv = ["review-backlog", "apply", "--registry", str(self.registry)]
        if who:
            argv += ["--token-file", str(self.tokens[who])]
        code, out = _run(self.store, [*argv, *extra])
        return code, json.loads(out) if out.strip() else {}

    def test_dry_run_writes_nothing(self):
        code, data = self._apply("--action", "reject", "--reason", "noise", "--bridge-noise")
        self.assertEqual(code, 0)
        self.assertFalse(data["execute"])
        self.assertEqual([r["outcome"] for r in data["results"]], ["dry_run"])
        self.assertEqual(_status(self.store, "noise"), "human_review")

    def test_execute_reject_goes_through_review_path(self):
        code, data = self._apply("--action", "reject", "--reason", "bridge noise",
                                 "--bridge-noise", "--execute")
        self.assertEqual(code, 0)
        self.assertEqual(data["applied"], 1)
        self.assertEqual(_status(self.store, "noise"), "rejected")
        self.assertEqual(_status(self.store, "a"), "human_review")
        with self.store.transaction() as c:
            ev = c.execute("SELECT count(*) n FROM memory_events WHERE event_type='candidate.rejected'"
                           ).fetchone()["n"]
        self.assertEqual(ev, 1)

    def test_execute_approve_commits(self):
        code, data = self._apply("--action", "approve", "--reason", "ok", "--ids", "a", "--execute")
        self.assertEqual(code, 0, data)
        res = data["results"][0]
        self.assertEqual(res["status"], "committed")
        self.assertTrue(res["fact_id"])
        self.assertEqual(self.store.get_fact(res["fact_id"])["human_status"], "confirmed")

    def test_rerun_is_idempotent(self):
        self._apply("--action", "reject", "--reason", "x", "--ids", "a", "--execute")
        code, data = self._apply("--action", "reject", "--reason", "x", "--ids", "a", "--execute")
        # a is no longer in the backlog, so a second run reports it, it does not double-write
        self.assertEqual(code, 1)
        self.assertEqual(data["results"][0]["outcome"], "failed")

    def test_partial_failure_is_nonzero(self):
        code, data = self._apply("--action", "reject", "--reason", "x", "--ids", "a,missing", "--execute")
        self.assertEqual(code, 1)
        outcomes = {r["candidate_id"]: r["outcome"] for r in data["results"]}
        self.assertEqual(outcomes, {"missing": "failed", "a": "applied"})
        self.assertEqual(_status(self.store, "a"), "rejected")

    def test_refuses_whole_backlog(self):
        code, _ = self._apply("--action", "reject", "--reason", "x", "--execute")
        self.assertEqual(code, 2)
        self.assertEqual(_status(self.store, "a"), "human_review")

    def test_requires_reviewer_token(self):
        # 1.9.2: a typed --actor is no longer enough to file a decision.
        code, _ = self._apply("--actor", "thor", "--action", "reject", "--reason", "x",
                              "--ids", "a", "--execute", who=None)
        self.assertEqual(code, 2)
        self.assertEqual(_status(self.store, "a"), "human_review")

    def test_token_without_review_scope_is_rejected(self):
        code, _ = self._apply("--action", "reject", "--reason", "x", "--ids", "a",
                              "--execute", who="odin")
        self.assertEqual(code, 2)
        self.assertEqual(_status(self.store, "a"), "human_review")

    def test_actor_must_match_token_principal(self):
        code, _ = self._apply("--actor", "mentor", "--action", "reject", "--reason", "x",
                              "--ids", "a", "--execute")
        self.assertEqual(code, 2)
        self.assertEqual(_status(self.store, "a"), "human_review")

    def test_decision_is_recorded_under_token_principal(self):
        code, _ = self._apply("--action", "reject", "--reason", "x", "--ids", "a", "--execute")
        self.assertEqual(code, 0)
        with self.store.transaction() as c:
            who = c.execute("SELECT reviewer_principal FROM review_actions WHERE candidate_id='a'"
                            ).fetchone()["reviewer_principal"]
        self.assertEqual(who, "thor")


if __name__ == "__main__":
    unittest.main()
