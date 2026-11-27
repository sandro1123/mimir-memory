#!/usr/bin/env python3
"""
Mimir 自动记忆 hook for Claude Code —— 部署到远程 Agent 机器上，让记忆沉淀从“主动调用”
变成“事件驱动”。

绑定三个 hook 事件（见 references/claude-code-auto-memory-hook.md）：
  Stop       —— 每轮回复结束（async）
  PreCompact —— 上下文压缩前（必须 async=false，否则压缩已经发生）
  SessionEnd —— 会话终止（async）

工作流：读 hook JSON → 从 transcript 抽本轮文本 → 用小模型判断价值并归类
        → 值得记则 POST /v8/facts → 全程失败静默。

部署前改这三处：PRINCIPAL（目标 owner）、TOKEN_FILE、LLM_MODEL。
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PRINCIPAL = os.environ.get("MIMIR_PRINCIPAL", "thor")  # claudecode retired 2026-11-24
MIMIR_HOST = os.environ.get("MIMIR_HOST", "127.0.0.1")  # remote hosts must set MIMIR_HOST
MIMIR_PORT = os.environ.get("MIMIR_PORT", "8456")
TOKEN_FILE = Path(os.environ.get("MIMIR_TOKEN_FILE", str(Path.home() / ".hermes" / "mimir" / "secrets" / "clients" / f"{PRINCIPAL}.token")))
LLM_BASE = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:20129")
LLM_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.environ.get("MIMIR_HOOK_LLM", "cbcn/glm-5.3-flash")

STATE_DIR = Path.home() / ".claude" / "mimir-hook"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG = STATE_DIR / "hook.log"
SEEN = STATE_DIR / "seen.json"

DOMAINS = ["infrastructure", "quant", "tech_support", "personal", "system", "knowledge"]
FACT_TYPES = ["event", "user_pref", "project_config", "pattern", "iron_rule", "reference"]

MIN_TEXT_LEN = 60
MAX_TEXT_LEN = 6000
MAX_WRITE_PER_HOUR = 6
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ibk_[a-z2-7]{20,}|BEGIN [A-Z ]*PRIVATE KEY|密码[:：]\s*\S{6,})"
)


def log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        if LOG.stat().st_size > 512_000:
            keep = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-2000:]
            LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_seen() -> dict:
    try:
        return json.loads(SEEN.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(d: dict) -> None:
    try:
        SEEN.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def extract_turn(transcript_path: str, fallback_msg: str = ""):
    """取 transcript 最后一轮 (user_text, assistant_text)。只读文件尾部避免大文件全量载入。"""
    asst = (fallback_msg or "").strip()
    user = ""
    try:
        p = Path(transcript_path).expanduser()
        if not p.is_file():
            return "", asst
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()
    except Exception as e:
        log("transcript read fail: %s" % e)
        return "", asst

    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rtype = rec.get("type")
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "\n".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        if rtype == "assistant" and not asst:
            asst = content.strip()
        elif rtype == "user" and not user:
            if content.strip().startswith("{") or "<local-command" in content:
                continue
            user = content.strip()
            if asst:
                break
    return user, asst


def call_llm(prompt: str, max_tokens: int = 600, timeout: int = 30) -> str:
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        LLM_BASE.rstrip("/") + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + LLM_KEY}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return (d["choices"][0]["message"].get("content") or "").strip()


def judge(user_text: str, asst_text: str):
    prompt = f"""你是记忆筛选器。判断下面这轮对话是否产生了值得跨会话长期保存的事实。

【对话内容】
<user>{user_text[:1500]}</user>
<assistant>{asst_text[:2500]}</assistant>

【值得记的标准】满足任一即可：
1. 主人明确表达了偏好、规矩、禁止事项
2. 定下了配置/端口/路径/模型等具体参数
3. 排查出一个非显而易见的根因或教训
4. 做出了跨会话仍有意义的决策

【不该记】闲聊、寒暄、任务中间进度、纯代码输出、一次性问答、对已有知识的复述。

【禁止】不要把密码、token、私钥写进内容。

如果值得记，输出严格 JSON（不要 markdown 代码块）：
{{"save": true, "items": [{{"domain": "六选一:{'/'.join(DOMAINS)}", "fact_type": "六选一:{'/'.join(FACT_TYPES)}", "content": "一句话说清的事实，保留关键参数"}}]}}

如果不值得记，输出：{{"save": false, "items": []}}

最多 2 条。content 控制在 200 字内。只输出 JSON。"""
    try:
        raw = call_llm(prompt)
    except Exception as e:
        log("judge llm fail: %s" % e)
        return []
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except Exception:
        return []
    if not d.get("save"):
        return []
    out = []
    for it in (d.get("items") or [])[:2]:
        dom, ft = it.get("domain"), it.get("fact_type")
        ct = (it.get("content") or "").strip()
        if dom not in DOMAINS or ft not in FACT_TYPES or len(ct) < 8:
            continue
        if SECRET_RE.search(ct):
            log("skip: sensitive content detected")
            continue
        out.append({"domain": dom, "fact_type": ft, "content": ct})
    return out


def write_mimir(domain: str, fact_type: str, content: str) -> bool:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        log("token read fail: %s" % e)
        return False
    body = json.dumps({
        "content": content,
        "owner_principal": PRINCIPAL,
        "domain": domain,
        "fact_type": fact_type,
        "visibility": "shared",
    }).encode()
    req = urllib.request.Request(
        "http://%s:%s/v8/facts" % (MIMIR_HOST, MIMIR_PORT), data=body,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode())
        log("WROTE %s/%s fact_id=%s :: %s" % (domain, fact_type, d.get("fact_id"), content[:60]))
        return True
    except urllib.error.HTTPError as e:
        log("mimir write HTTP %s: %s" % (e.code, e.read().decode()[:200]))
    except Exception as e:
        log("mimir write fail: %s" % e)
    return False


def within_rate_limit(seen: dict) -> bool:
    now = time.time()
    recent = [v for v in seen.values() if isinstance(v, (int, float)) and now - v < 3600]
    return len(recent) < MAX_WRITE_PER_HOUR


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    event = payload.get("hook_event_name", "")
    session = payload.get("session_id", "unknown")
    user_text, asst_text = extract_turn(
        payload.get("transcript_path", ""), payload.get("last_assistant_message", ""))
    combined = (user_text + "\n" + asst_text).strip()

    if len(combined) < MIN_TEXT_LEN:
        log("[%s] skip: too short (%d)" % (event, len(combined)))
        return
    combined = combined[:MAX_TEXT_LEN]

    key = "%s:%s" % (session, hashlib.sha256(
        combined.encode("utf-8", errors="ignore")).hexdigest()[:16])
    seen = load_seen()
    if key in seen:
        log("[%s] skip: already processed %s" % (event, key))
        return
    if not within_rate_limit(seen):
        log("[%s] skip: rate limit reached" % event)
        return

    items = judge(user_text, asst_text)
    wrote = 0
    for it in items:
        if write_mimir(it["domain"], it["fact_type"], it["content"]):
            wrote += 1

    seen[key] = time.time()
    cutoff = time.time() - 7 * 86400
    seen = {k: v for k, v in seen.items() if not isinstance(v, (int, float)) or v > cutoff}
    save_seen(seen)
    log("[%s] done: wrote %d/%d" % (event, wrote, len(items))
        if items else "[%s] judged: nothing worth saving" % event)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL: %s" % e)
    sys.exit(0)  # 永不阻塞宿主
