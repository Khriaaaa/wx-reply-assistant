#!/usr/bin/env python3
"""编排层 v1：读聊天记录 -> 拼 prompt -> 调 LLM -> 校验 -> 追加 suggestions.jsonl

用法:
  python3 generate.py [--session NAME] [--limit 20]
                                            [--dry-run] [--out PATH] [--verbose]
只读 store/messages.jsonl，只写 store/suggestions.jsonl（或 --out 指定）。
不发消息、不碰键鼠。API key 不会被打印。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = ROOT / "store" / "messages.jsonl"
SUGGESTIONS = ROOT / "store" / "suggestions.jsonl"
PROMPT = ROOT / "prompts" / "reply_system.md"

# provider 凭据来自 Hermes 的 config.yaml。
# 容器里用绝对路径；放到虚机/别的机器上跑时那个路径不存在，
# 于是支持两种覆盖：环境变量 WXREPLY_CONFIG，或项目根下的 config.local.yaml。
CONFIG = Path("/opt/data/config.yaml")
_env_cfg = os.environ.get("WXREPLY_CONFIG")
if _env_cfg:
    CONFIG = Path(_env_cfg)
elif (ROOT / "config.local.yaml").exists():
    CONFIG = ROOT / "config.local.yaml"

# 链首模型：只影响这个工具，不动 Hermes 主链路（Hermes 自己继续走 deepseek-v4.1-flash）。
# 传 --provider none 可回退到 config.yaml 的 model.default + fallback_providers。
# 2026-10-05 起用 mimo-v2.6-flash：带推理、直接出 JSON，不受 commandcode 5 小时窗口影响。
# 想要更强的用 --model mimo-v2.6-pro（实测 9.0s / 181 out tok，flash 是 5.7s / 115）。
# 换成自己的 provider / 模型：设 WXREPLY_LEAD_PROVIDER / WXREPLY_LEAD_MODEL。
# provider 名的写法跟 config.yaml 里 provider 段的键一致（形如 custom:xxx）。
LEAD_PROVIDER = os.environ.get("WXREPLY_LEAD_PROVIDER", "custom:mimoplan")
LEAD_MODEL = os.environ.get("WXREPLY_LEAD_MODEL", "mimo-v2.6-flash")

# 整条候选链的总时间预算（秒），可用 WXREPLY_BUDGET 覆盖。
# 链上每个 provider 的单发超时是 120s，链长 4 个时最坏 480s —— 比调用方的
# subprocess 超时（watch / 面板自检都是 180s 上下）还长，于是 generate 会在
# 还没轮到备用 provider 时就被 SIGKILL，60 秒后再原样重试一遍，永远轮不到降级。
# 所以这里自己管总预算：每个 provider 只分到「剩余预算」，用完就带着每个候选的
# 真实原因退出。调用方的超时取 budget + 30 即可，不需要跟着链长改。
BUDGET_SEC = float(os.environ.get("WXREPLY_BUDGET", "150"))


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_messages(path=None):
    src = Path(path) if path else MESSAGES
    if not src.exists():
        die(f"找不到 {src}")
    msgs = []
    for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"警告: messages.jsonl 第 {i} 行解析失败: {e}", file=sys.stderr)
    if not msgs:
        die("messages.jsonl 里没有消息")
    return msgs


def build_transcript(msgs, session, limit):
    sel = [m for m in msgs if m.get("session") == session][-limit:]
    if not sel:
        die(f"会话 {session!r} 没有消息")
    lines = []
    for m in sel:
        s = m.get("sender")
        if s == "me":
            who = "我"
        elif s == "them":
            who = f"对方({session})"
        else:
            who = "未知"
        t = m.get("time_hint") or ""
        lines.append(f"[{t}] {who}: {m.get('text', '')}" if t else f"{who}: {m.get('text', '')}")
    # 顺带把最后一条「对方」消息的指纹带出去：自检线程靠它判断「建议是不是已经
    # 跟上最新消息」。故意不看最后一条是不是我发的 —— 我方消息不该触发新一轮生成
    # （watch 侧同样只认 sender=="them"），否则我自己每发一条消息，自检就白烧一次模型。
    last_fp = next((m.get("fp") for m in reversed(sel)
                    if m.get("sender") == "them" and m.get("fp")), None)
    return "\n".join(lines), len(sel), last_fp


def _provider_creds(cfg, prov_ref):
    """prov_ref 形如 custom:commandcode；返回 (base_url, api_key)。"""
    name = prov_ref.split(":", 1)[1] if ":" in prov_ref else prov_ref
    for p in cfg.get("custom_providers") or []:
        if p.get("name") == name:
            return (p.get("base_url") or "").rstrip("/"), p.get("api_key")
    return None, None


def load_providers(override=None):
    """拼候选链：override(如有) -> model(主) -> fallback_providers(依次)。

    返回 [(label, base_url, api_key, model), ...]
    实测必要：commandcode 的 5 小时窗口被吃光后主模型直接 429，
    没有这条链生成就整个废掉。
    override 形如 ("custom:mimoplan", "mimo-v2.5")，只排链首、不写配置文件，
    所以换这个工具用哪家模型不动 Hermes 主链路。
    """
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    chain, seen = [], set()

    if override:
        prov_ref, mid = override
        base, key = _provider_creds(cfg, prov_ref)
        if not base or not key:
            die(f"指定 provider {prov_ref} 在 config.yaml 里查不到 base_url / api_key")
        chain.append((prov_ref, base.rstrip("/"), key, mid))

    m = cfg.get("model") or {}
    if m.get("default") and m.get("provider"):
        base, key = _provider_creds(cfg, m["provider"])
        base = (m.get("base_url") or base or "").rstrip("/")
        if base and key:
            chain.append((m["provider"], base, key, m["default"]))

    for fb in cfg.get("fallback_providers") or []:
        prov, mid = fb.get("provider"), fb.get("model")
        if not prov or not mid:
            continue
        base, key = _provider_creds(cfg, prov)
        base = (fb.get("base_url") or base or "").rstrip("/")
        if not base or not key:
            continue
        sig = (base, mid)
        if sig in seen:
            continue
        seen.add(sig)
        chain.append((prov, base, key, mid))

    if not chain:
        die("config.yaml 里没解析出任何可用的 provider")
    return chain


def _try_one(base, key, model, prompt_text, verbose, timeout=120):
    """打一发；成功返回 content 字符串，失败抛 Exception（带真实原因）。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.7,
        "max_tokens": 2000,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # 裸 urllib 不带 UA 会被 Cloudflare 拦成 403 code 1010
        "User-Agent": "OpenAI/Python 1.0.0",
    }
    # opencode Go 强制要求 x-opencode-session，缺了直接 400 MissingSessionID
    #（Hermes 自己会注入这个头，裸脚本得手写）
    if "opencode.ai" in base:
        headers["x-opencode-session"] = "hermes-wxreply"
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}") from None
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}") from None
    if verbose:
        print(f"[verbose] {model} 响应长度 {len(raw)}", file=sys.stderr)
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"响应结构异常 ({e})，原始内容: {raw[:400]}") from None
    if not content or not content.strip():
        # 实测：带推理的模型 max_tokens 太小时 content 会是空串，也算失败
        raise RuntimeError("返回内容为空")
    return content


def call_llm(prompt_text, verbose, override=None, budget=None):
    """依次尝试候选 provider，第一个成功的胜出。全挂才报错，并带上每一条的真实原因。

    每个候选只分到「总预算剩余」这么多时间，保证整轮一定在预算内结束 —— 否则链尾的
    备用 provider 永远轮不到（外层 subprocess 先把 generate 杀了）。
    """
    errors = []
    budget = BUDGET_SEC if budget is None else float(budget)
    deadline = time.monotonic() + budget
    for label, base, key, model in load_providers(override):
        left = deadline - time.monotonic()
        if left <= 2:            # 剩不到 2 秒就发出去也只会超时，直接跳过

            errors.append(f"  - {label} / {model}: 跳过（总预算 {budget:.0f}s 已用完）")
            continue
        try:
            content = _try_one(base, key, model, prompt_text, verbose, timeout=min(120.0, left))
            if verbose and errors:
                print(f"[verbose] 主 provider 失败、已降级到 {label}/{model}", file=sys.stderr)
            return content, label, model
        except Exception as e:
            errors.append(f"  - {label} / {model}: {e}")
            if verbose:
                print(f"[verbose] {label} 失败: {e}", file=sys.stderr)
    die("所有 provider 都失败了:\n" + "\n".join(errors))


def parse_and_validate(content):
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        die(f"JSON 解析失败: {e}\n原始返回:\n{content}")
    err = None
    if not isinstance(obj, dict):
        err = "顶层不是对象"
    else:
        if not isinstance(obj.get("need"), str) or not obj["need"].strip():
            err = "缺 need（对方此刻可能需要什么）"
        c = obj.get("candidates")
        if not isinstance(c, list) or len(c) != 3:
            err = "candidates 必须是长度 3 的数组"
        else:
            for i, it in enumerate(c):
                if not isinstance(it, dict) or not all(k in it for k in ("text", "style", "score")):
                    err = f"candidates[{i}] 缺 text/style/score"
                    break
                t = it["text"]
                if not isinstance(t, str) or not t.strip():
                    err = f"candidates[{i}].text 为空"
                    break
                if "\n" in t or "\r" in t:
                    err = f"candidates[{i}].text 含换行"
                    break
    if err:
        die(f"校验失败: {err}\n原始返回:\n{content}")
    # tension 是展示用的弱字段：模型没给或给了越界值也不该让整轮生成失败，夹到 1-9
    try:
        t = int(obj.get("tension"))
    except (TypeError, ValueError):
        t = 5
    obj["tension"] = max(1, min(9, t))
    return obj


def main():
    ap = argparse.ArgumentParser(description="根据微信聊天记录生成 3 条候选回复（不发送）")
    ap.add_argument("--session", help="会话名，默认取最后一条消息的会话")
    ap.add_argument("--limit", type=int, default=20, help="取最近多少条消息，默认 20")
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调 API")
    ap.add_argument("--provider", default=LEAD_PROVIDER,
                    help="链首 provider，传 none 走 config.yaml 默认链")
    ap.add_argument("--model", default=LEAD_MODEL, help="链首模型名，传 none 走默认链")
    ap.add_argument("--out", help="结果追加到此文件，默认 store/suggestions.jsonl")
    ap.add_argument("--messages", help="聊天记录文件，默认 store/messages.jsonl")
    ap.add_argument("--budget", type=float, default=BUDGET_SEC,
                    help=f"整条候选链的总时间预算（秒），默认 {BUDGET_SEC:.0f}")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    msgs = load_messages(a.messages)
    # 没指定会话时优先取「最后一条对方消息」的会话：那才是需要回的那条。
    session = a.session or next(
        (m.get("session") for m in reversed(msgs)
         if m.get("sender") == "them" and m.get("session")), msgs[-1].get("session"))
    transcript, n, last_fp = build_transcript(msgs, session, a.limit)
    prompt = PROMPT.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + transcript + "\n"

    if a.dry_run:
        print(prompt)
        return
    if a.verbose:
        print(f"[verbose] session={session} 消息数={n}", file=sys.stderr)

    override = None
    if a.provider not in ("", "none") and a.model not in ("", "none"):
        override = (a.provider, a.model)
    content, label, used_model = call_llm(prompt, a.verbose, override, budget=a.budget)
    obj = parse_and_validate(content)
    rec = {
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session,
        "provider": label,
        "model": used_model,
        "strategy": obj.get("strategy"),
        "intent": obj.get("intent"),
        "need": obj.get("need"),
        "tension": obj.get("tension"),
        "candidates": obj["candidates"],
        "source_msg_count": n,
        "source_last_fp": last_fp,
    }
    out = Path(a.out) if a.out else SUGGESTIONS
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
