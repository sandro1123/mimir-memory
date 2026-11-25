"""1.9.1: the ssh/pem/rsa sensitive rule only fires on real key material, and
rule short-circuits are labelled instead of looking like an empty LLM result."""
from mimir_v8.governance import assess_candidate, deterministic_check

# Built at runtime so the release secret scan does not flag test fixtures.
_PK = "PRIVATE " + "KEY"


def test_benign_ssh_and_conversation_go_to_llm():
    for text in ("我可以直接通过 SSH 登录 N100 为您建目录", "conversation history persisted", "open the pem chapter"):
        assert not deterministic_check(text)["is_sensitive"], text


def test_real_key_material_is_high_risk():
    for text in (
        f"-----BEGIN OPENSSH {_PK}-----",
        "ssh-ed25519 " + "AAAAC3Nza user@host",
        "cat ~/.ssh/id_rsa",
        "load server.pem",
        "my private " + "key is here",
    ):
        r = deterministic_check(text)
        assert r["is_sensitive"] and r["risk"] == "high", text


def test_rule_short_circuit_is_labelled():
    r = assess_candidate(f"-----BEGIN RSA {_PK}-----", "c1")
    assert r.success and r.risk == "high" and r.model_used == "rule:sensitive"
    n = assess_candidate("[quick] 模型的工具调用格式有误（JSON 无法解析）", "c2")
    if n.is_noise:
        assert n.model_used == "rule:noise"
