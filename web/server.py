#!/usr/bin/env python3
"""面板层后端 v1（只读，仅标准库）

用法:
  python3 web/server.py [--port 8801] [--host 0.0.0.0]
路径相对脚本位置，不依赖 cwd。数据在 ../store/。
密码: ../store/.panel_password（不存在则随机生成，仅首次生成时在日志打印一次）
接口: GET / | /login | /api/state | /api/health | /api/setup ; POST /login | /api/generate | /api/fill | /api/setup
/api/setup 是「首次配置」：网页上挑一家厂商（预置国内主流厂商的 OpenAI 兼容地址，
见 orchestrator/providers_cn.py）、填 Key、填模型，服务端真连一次测通再落盘。
Key 存在 store/.llm.yaml（0600，不进仓库）里，接口只回显尾四位，绝不回传明文。
/api/fill 把某条候选填进 NAS 虚机里微信的输入框（只填不发送，见 ../tools/fill_vm.py）。
**默认关闭**：目标就是「只给建议、不替你回」。要开就设环境变量 HERMES_PANEL_ALLOW_FILL=1
或放一个 store/.allow_fill 文件，重启即生效；关闭时接口一律 403。
除 /login 与 /api/health 外均需登录。
"""
import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

import yaml

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
STORE = Path(os.environ.get("WXREPLY_STORE", str(ROOT / "store")))   # 换目录只为了测试
MESSAGES = STORE / "messages.jsonl"
SUGGESTIONS = STORE / "suggestions.jsonl"
HEARTBEAT = STORE / "cache" / "heartbeat.txt"
GENERATE = ROOT / "orchestrator" / "generate.py"
COLLECTOR = ROOT / "collector" / "wx_collector.py"
CHECK_STATE = STORE / "checker.json"
CHECK_TIMEOUT = 240          # 一轮自检里「拉起虚机采集 + 搬运」的上限
# generate 自己管一个总预算（见 orchestrator/generate.py 的 BUDGET_SEC），
# 这里只要比它多一点即可：比预算小的话，generate 还没轮到备用 provider 就被杀了。
GEN_BUDGET = float(os.environ.get("WXREPLY_BUDGET", "150"))
GEN_TIMEOUT = GEN_BUDGET + 30
PW_FILE = STORE / ".panel_password"
SECRET_FILE = STORE / ".panel_secret"
# 「首次配置」写到这里：单独的 0600 私密文件，不进仓库、也不动用户的 config.local.yaml
LLM_CFG = Path(os.environ.get("WXREPLY_LLM_CONFIG", str(STORE / ".llm.yaml")))
COOKIE = "panel_session"
COOKIE_TTL = 7 * 86400

sys.path.insert(0, str(ROOT / "tools"))
import fill_vm                                   # noqa: E402  填入虚拟通道
_fill_lock = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 后台自检
def check_enabled():
    """自检开关，默认开：面板自己盯新聊天记录，不靠外面先把采集起好。
    要关就设 HERMES_PANEL_NO_CHECK=1，或放一个 store/.no_check 文件。"""
    if os.environ.get("HERMES_PANEL_NO_CHECK") == "1":
        return False
    return not (STORE / ".no_check").exists()


def read_check():
    try:
        return json.loads(CHECK_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_check(**kw):
    cur = read_check()
    cur.update(kw)
    cur["updated_utc"] = utcnow()
    tmp = CHECK_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CHECK_STATE)


def newest_fp():
    """最后一条「对方」消息的指纹；没有对方消息时返回 None。

    必须和 generate.py 写进 source_last_fp 的取法完全一致（都取最后一条 them 的 fp）。
    这里先后错过两次：先只认 sender=="them" 而生成侧记的是最后一条（可能是 me），
    两边永远对不上，每一轮自检都白烧一次模型；改成「不论谁说的最后一条」之后，
    我自己每发一条消息又会让指纹变化、自检再生成一轮 —— 同样没有新的对方消息。
    现在两边都只认 them。
    """
    for m in reversed(read_jsonl(MESSAGES)):
        if m.get("sender") == "them" and m.get("fp"):
            return m["fp"]
    return None


def check_round():
    """跑一轮自检：确保虚机采集在跑 -> 搬运一次 -> 有新消息就重新生成。

    返回 True 表示这一轮真的产出了新建议。
    """
    r = subprocess.run([sys.executable, str(COLLECTOR), "check", "--quiet"],
                       capture_output=True, text=True, timeout=CHECK_TIMEOUT, cwd=str(ROOT))
    out = (r.stdout or "") + (r.stderr or "")
    guest_running = None
    guest_started = None
    m = re.search(r"CHECK_JSON=(\{.*?\})", out)
    if m:
        try:
            _j = json.loads(m.group(1))
            guest_running = _j.get("running")
            guest_started = _j.get("started")
        except Exception:
            pass
    if r.returncode != 0:
        write_check(last_check_utc=utcnow(), last_result="error", guest_running=guest_running, guest_started=guest_started,
                    error=(out.strip()[-300:] or f"采集退出码 {r.returncode}"))
        return False

    msgs = read_jsonl(MESSAGES)
    fp = newest_fp()
    recs = read_jsonl(SUGGESTIONS)
    last = recs[-1] if recs else None
    if not fp:
        write_check(last_check_utc=utcnow(), last_result="no_message", guest_running=guest_running, guest_started=guest_started,
                    error=None, msgs=len(msgs))
        return False
    if last and last.get("source_last_fp") == fp:
        write_check(last_check_utc=utcnow(), last_result="no_change", guest_running=guest_running, guest_started=guest_started,
                    error=None, msgs=len(msgs))
        return False

    # 会话规则只此一份：generate.pick_session()（「最后一条对方消息」的会话）
    session = pick_session(msgs)
    if not _gen_lock.acquire(blocking=False):
        # 手动点了「生成」或上一轮自检还在跑：这轮让出，别两条 generate 并发。
        write_check(last_check_utc=utcnow(), last_result="busy", guest_running=guest_running,
                    guest_started=guest_started, error=None, msgs=len(msgs))
        return False
    cmd = [sys.executable, str(GENERATE)]
    if session:
        cmd += ["--session", str(session)]
    try:
        rr = subprocess.run(cmd, capture_output=True, text=True, timeout=GEN_TIMEOUT, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        write_check(last_check_utc=utcnow(), last_result="error", guest_running=guest_running, guest_started=guest_started,
                    error=f"生成超时（{GEN_TIMEOUT:.0f} 秒）")
        return False
    finally:
        _gen_lock.release()
    if rr.returncode == 75:      # 另一处（watch 或手动点生成）正拿着生成锁，这轮不算错
        write_check(last_check_utc=utcnow(), last_result="busy", guest_running=guest_running,
                    guest_started=guest_started, error=None, msgs=len(msgs))
        return False
    if rr.returncode != 0:
        write_check(last_check_utc=utcnow(), last_result="error", guest_running=guest_running, guest_started=guest_started,
                    error=(rr.stderr or rr.stdout or f"退出码 {rr.returncode}").strip()[-300:])
        return False
    write_check(last_check_utc=utcnow(), last_result="updated", guest_running=guest_running, guest_started=guest_started,
                error=None, msgs=len(msgs), last_change_utc=utcnow(), last_session=session)
    print(f"[check] 有新消息，已为 {session} 重新生成建议", flush=True)
    return True


def checker_loop(interval):
    time.sleep(3)                     # 让面板先起来
    while True:
        try:
            if check_enabled():
                check_round()
        except Exception as e:
            try:
                write_check(last_check_utc=utcnow(), last_result="error",
                            error=f"{type(e).__name__}: {e}")
            except Exception:
                pass
        time.sleep(interval)


def fill_enabled():
    """填入通道开关，默认关：目标是只给建议，不替用户回。"""
    if os.environ.get("HERMES_PANEL_ALLOW_FILL") == "1":
        return True
    return (STORE / ".allow_fill").exists()

_fails = {}  # ip -> (count, first_ts)
# _fails 的「读计数 -> 判上限 -> 写计数」必须整段互斥：不加锁时并发失败请求
# 各自读到同一个 cnt 再各自写回 cnt+1，计数互相覆盖，5 次上限形同虚设。
_fails_lock = threading.Lock()
# 生成任务全局互斥：后台自检线程与手动 /api/generate 不能同时拉起 generate。
_gen_lock = threading.Lock()


def _secret_file(path, nbytes, announce=False, label=""):
    """读凭据文件；不存在、或存在但内容为空时重新生成。

    空串绝不能放行：空口令等于面板无密码（空 == 空 的 compare_digest 也会通过），
    空 HMAC key 等于 cookie 签名谁都能伪造。文件被 truncate、或上次写入被打断，
    都会留下这种「存在但为空」的状态，所以要把空内容当成损坏来处理。
    """
    if path.exists():
        os.chmod(path, 0o600)
        val = path.read_text(encoding="utf-8").strip()
        if val:
            return val
        print(f"[panel] 凭据文件 {path.name} 内容为空（被截断或写坏），重新生成", flush=True)
    STORE.mkdir(parents=True, exist_ok=True)
    val = secrets.token_urlsafe(nbytes)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(val + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)   # 原子落盘：不会留下写一半的空/半截凭据文件
    if announce:
        # 不打印内容：stdout 会被追加进 store/logs/panel.log，等于把口令明文落盘
        print(f"[panel] 已生成{label or '面板密钥'}，写到 store/{path.name}", flush=True)
    return val


def load_creds():
    pw = _secret_file(PW_FILE, 12, True, "面板密码")
    sec = _secret_file(SECRET_FILE, 32)
    return pw, sec.encode()


PASSWORD, SECRET = "", b""


def make_cookie():
    exp = str(int(time.time()) + COOKIE_TTL)
    nonce = secrets.token_hex(8)
    msg = f"{exp}.{nonce}"
    sig = hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def check_cookie(val):
    try:
        exp, nonce, sig = val.split(".")
        good = hmac.new(SECRET, f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


_jsonl_cache = {}       # str(path) -> ((mtime_ns, size), [rows])


def read_jsonl(path):
    """读 jsonl；按 (mtime, size) 缓存解析结果。

    /api/state 每 5 秒来一次，一次要读 messages + suggestions 两遍，watch 每 2 秒
    也整读一遍 —— 文件没变时重复解析纯属白烧 CPU，文件一大就看得出来。文件被改
    则 mtime/size 必变，缓存自然失效，不需要谁手动清。
    """
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    hit = _jsonl_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    _jsonl_cache[str(path)] = (key, out)
    return out


def pick_session(msgs):
    """会话规则只有一份，在 generate.py 的 pick_session() 里；这里只是转发。

    面板以前自己写了一份（取 msgs[-1]，最后一条不论谁发的），和生成侧（取最后一条
    对方消息的会话）不一致：我在 B 会话回最后一句，面板就切到 B，而 A 会话新消息
    生成的建议会被按会话过滤掉，界面上成了「明明有建议却显示还没有生成」。
    """
    try:
        return _gen_mod().pick_session(msgs)
    except Exception as e:      # 生成侧加载不出来也不能让面板 500
        print(f"[panel] 取会话失败，退回最后一条消息: {e}", flush=True)
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("session"):
                return m["session"]
        return None


def state():
    msgs = read_jsonl(MESSAGES)
    recs = read_jsonl(SUGGESTIONS)
    sug = recs[-1] if recs else None
    session = pick_session(msgs) or (sug or {}).get("session")
    # 建议必须属于当前展示的这个会话：手动给别的会话生成过一次之后，
    # 卡片上「对方最近说 / 对话参考 / 候选」会整块错配到另一个人身上（等于给错人出主意）
    if sug and sug.get("session") != session:
        sug = None
    shown = [m for m in msgs if m.get("session") == session][-30:]
    last_other = None
    for m in reversed(shown):
        if m.get("sender") == "them":
            last_other = {"text": m.get("text") or "", "time_hint": m.get("time_hint") or ""}
            break
    try:
        collecting = time.time() - HEARTBEAT.stat().st_mtime <= 60
    except OSError:
        collecting = False
    return {
        "session": session,
        "messages": shown,
        "last_other": last_other,
        "suggestions": sug.get("candidates", []) if sug else [],
        "suggestion_meta": {k: sug.get(k) for k in
                            ("ts_utc", "session", "strategy", "intent", "need", "tension",
                             "source_last_fp", "model")} if sug else None,
        "collecting": collecting,
        "fill_enabled": fill_enabled(),
        "check": read_check(),
        "check_enabled": check_enabled(),
    }


# ---------------------------------------------------------------- 首次配置（模型接口）
LLM_ENTRY = "wxreply"        # 写进 custom_providers 的那条的名字
sys.path.insert(0, str(ROOT / "orchestrator"))
import providers_cn          # noqa: E402  国内厂商的 OpenAI 兼容地址预置表


def _hint(key):
    """只回显尾四位。页面上要能看出「填过了」，但不能把 Key 还回去。"""
    k = (key or "").strip()
    if not k:
        return ""
    return ("…" + k[-4:]) if len(k) > 8 else "已填"


def _load_generate():
    """按文件加载 generate.py，只为复用它那套「候选配置文件」的解析逻辑。

    面板自己再写一份的话迟早错开：网页说已配置、生成说找不到 provider。
    会话规则（pick_session）也走这里，保证展示的会话和生成的会话是同一条规则。
    """
    spec = importlib.util.spec_from_file_location("wx_generate", GENERATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GEN_MOD = None


def _gen_mod():
    """按需加载并缓存 generate 模块：state() 每次轮询都要用它的 pick_session，
    不能每次重新 exec 一遍两万多字节的源码。"""
    global _GEN_MOD
    if _GEN_MOD is None:
        _GEN_MOD = _load_generate()
    return _GEN_MOD


def read_llm_cfg():
    try:
        if LLM_CFG.exists():
            os.chmod(LLM_CFG, 0o600)
            return yaml.safe_load(LLM_CFG.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[panel] 读 {LLM_CFG.name} 失败: {e}", flush=True)
    return {}


def saved_provider():
    """面板自己写的那条配置（没有就 None）"""
    cfg = read_llm_cfg()
    m = cfg.get("model") or {}
    ref = str(m.get("provider") or "")
    name = ref.split(":", 1)[-1] if ref else ""
    if not name:
        return None
    entry = next((p for p in (cfg.get("custom_providers") or [])
                  if isinstance(p, dict) and p.get("name") == name), None)
    return {
        "name": name,
        "base_url": (entry or {}).get("base_url") or "",
        "api_key": (entry or {}).get("api_key") or "",
        "model": m.get("default") or "",
    }


def resolved_chain():
    """问 generate.py：现在到底能拼出哪条候选链（跟生成时用的是同一套逻辑）"""
    try:
        chain = _load_generate().load_providers()
    except SystemExit:
        return []
    except Exception as e:
        print(f"[panel] 解析 provider 链失败: {type(e).__name__}: {e}", flush=True)
        return []
    return [{"provider": lbl, "base_url": base, "model": mid} for lbl, base, _k, mid in chain]


def setup_state():
    saved = saved_provider() or {}
    chain = resolved_chain()
    return {
        "configured": bool(chain),
        "saved": {
            "provider": saved.get("name") or "",
            "base_url": saved.get("base_url") or "",
            "model": saved.get("model") or "",
            "key_hint": _hint(saved.get("api_key")),
            "key_set": bool((saved.get("api_key") or "").strip()),
        },
        "chain": chain,
        "presets": providers_cn.for_panel(),
        "verified_at": providers_cn.VERIFIED_AT,
        "config_file": LLM_CFG.name,
    }


def _err_text(status, body):
    if status == 0:
        return "连不上：域名解析不到或网络不通"
    txt = ""
    try:
        j = json.loads(body.decode("utf-8", "replace"))
        e = j.get("error") if isinstance(j, dict) else None
        if isinstance(e, dict):
            txt = e.get("message") or e.get("code") or json.dumps(e, ensure_ascii=False)[:200]
        elif e:
            txt = str(e)
        else:
            txt = (j.get("message") or j.get("msg") or "") if isinstance(j, dict) else ""
    except Exception:
        txt = body.decode("utf-8", "replace").strip()
    txt = re.sub(r"\s+", " ", txt)[:300]
    return f"HTTP {status}：{txt}" if txt else f"HTTP {status}"


def _http_json(url, key=None, payload=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "wx-reply-assistant/panel")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(300000)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(8000)
        except Exception:
            return e.code, b""
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}".encode()


def probe_endpoint(base, key, model=""):
    """真连一次，别只看语法。

    先拉模型列表（/models，不花 token）—— 多数国内厂商都实现了，顺便把
    模型名列表带回来给页面下拉用。列表拿不到再用 1 个 token 试一次对话。
    """
    base = (base or "").rstrip("/")
    models, detail = [], ""
    status, body = _http_json(base + "/models", key, timeout=15)
    if status == 200:
        try:
            j = json.loads(body.decode("utf-8", "replace"))
            models = [str(x.get("id")) for x in (j.get("data") or [])
                      if isinstance(x, dict) and x.get("id")]
        except Exception:
            models = []
        if models:
            return True, models, f"接口通了，你这账号下有 {len(models)} 个模型"
        if not model:
            return True, models, "接口通了（这个厂商没给模型列表，得手填模型名）"
    if not model:
        return False, models, _err_text(status, body)
    detail = _err_text(status, body)
    status2, body2 = _http_json(base + "/chat/completions", key,
                                {"model": model,
                                 "messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 1}, timeout=30)
    if status2 == 200:
        return True, models, "接口通了（1 个 token 的对话测试也过了）"
    return False, models, _err_text(status2, body2) or detail


def save_llm_cfg(base, key, model, entry_name=LLM_ENTRY):
    """原子落盘 0600。只动 model 段和 custom_providers 里自己那条，别的不碰。"""
    LLM_CFG.parent.mkdir(parents=True, exist_ok=True)
    cfg = read_llm_cfg()
    provs = [p for p in (cfg.get("custom_providers") or [])
             if isinstance(p, dict) and p.get("name") != entry_name]
    provs.append({"name": entry_name, "base_url": base.rstrip("/"), "api_key": key})
    cfg["custom_providers"] = provs
    m = dict(cfg.get("model") or {})
    m.update({"default": model, "provider": "custom:" + entry_name, "base_url": base.rstrip("/")})
    cfg["model"] = m
    tmp = LLM_CFG.with_name(LLM_CFG.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# 面板「首次配置」写的模型接口凭据 —— 属于本机私密文件，不进仓库\n")
        f.write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, LLM_CFG)
    return LLM_CFG


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录</title>
<style>body{font-family:sans-serif;background:#f3f4f6;display:flex;justify-content:center;padding-top:20vh}
form{background:#fff;padding:24px;border-radius:10px;box-shadow:0 1px 4px #0002;width:280px}
input,button{width:100%;box-sizing:border-box;padding:10px;margin-top:10px;font-size:16px}
button{background:#07c160;color:#fff;border:0;border-radius:6px}.e{color:#c00;font-size:14px}</style></head>
<body><form method="post" action="/login"><b>回复助手面板</b>%ERR%
<input type="password" name="password" placeholder="密码" autofocus required>
<button>登录</button></form></body></html>"""


class _BodyTooLarge(Exception):
    """客户端声明的 body 超过上限。"""


class H(BaseHTTPRequestHandler):
    server_version = "panel"
    # ThreadingHTTPServer 是「一连接一线程、线程数无上限」，而读请求行/头/体
    # 每一步都是阻塞 recv。不给超时，半开连接（连上不发数据、或只发半个请求）
    # 就能无限期占住线程，几百个即可把面板拖死。30 秒后 BaseHTTPRequestHandler
    # 自己关连接。
    timeout = 30

    def log_message(self, fmt, *a):
        sys.stderr.write("[panel] %s %s\n" % (self.address_string(), fmt % a))

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and check_cookie(v):
                return True
        return False

    def _deny(self, api):
        if api:
            self._send(401, {"error": "未登录"})
        else:
            self._send(302, b"", headers={"Location": "/login"})

    def _oops(self, e):
        """兜底：任何没接住的异常都要给客户端一个回应。

        不接的话 BaseHTTPRequestHandler 直接断连接，浏览器只看到
        「Failed to fetch」，面板上什么线索都没有。
        """
        import traceback
        traceback.print_exc()
        try:
            self._send(500, {"error": f"面板内部错误：{type(e).__name__}: {e}"})
        except Exception:
            pass

    def do_GET(self):
        try:
            return self._route_get()
        except _BodyTooLarge:
            return self._send(413, {"error": "请求体过大（上限 64KB）"})
        except Exception as e:
            return self._oops(e)

    def _route_get(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            return self._send(200, {"ok": True})
        if path == "/login":
            return self._send(200, LOGIN_HTML.replace("%ERR%", ""), "text/html; charset=utf-8")
        api = path.startswith("/api/")
        if not self._authed():
            return self._deny(api)
        if path == "/api/state":
            return self._send(200, state())
        if path == "/api/setup":
            return self._send(200, setup_state())
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if api:
            return self._send(404, {"error": "not found"})
        self._serve_static(unquote(path).lstrip("/"))

    def _serve_static(self, rel):
        try:
            p = (WEB / rel).resolve()
            p.relative_to(WEB)
        except (ValueError, OSError):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        if not p.is_file() or p.name == "server.py" or p.name.startswith("."):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(p.suffix, "application/octet-stream")
        self._send(200, p.read_bytes(), ctype)

    def _body(self):
        """读请求体。

        Content-Length 不是数字会让 int() 抛 ValueError（请求直接断在半路、刷 traceback），
        是负数会让 read(-5) 等价于 read(-1) —— 一直读到 EOF 才返回，把 worker 线程挂住
        （ThreadingHTTPServer 线程没有上限，可以这样被耗光）。两种都按 0 处理。
        """
        raw = self.headers.get("Content-Length") or "0"
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return b""
        if n > 65536:
            # 只读前 64KB 的话，剩下的字节留在 socket 缓冲里：对端复用 keep-alive
            # 连接时它们会被当成下一个请求的请求行解析（错乱 + 400），不复用又可能
            # 把线程吊在残留数据上。直接拒收并断开，最省事也最安全。
            self.close_connection = True
            raise _BodyTooLarge()
        return self.rfile.read(n) or b""

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/login":
                return self._login()
            if not self._authed():
                return self._deny(path.startswith("/api/"))
            if path == "/api/generate":
                return self._generate()
            if path == "/api/setup":
                return self._setup()
            if path == "/api/fill":
                return self._fill()
            self._send(404, {"error": "not found"})
        except _BodyTooLarge:
            return self._send(413, {"error": "请求体过大（上限 64KB）"})
        except Exception as e:
            return self._oops(e)

    def _login(self):
        ip = self.client_address[0]
        form = parse_qs(self._body().decode("utf-8", "replace"))
        pw = (form.get("password") or [""])[0]
        # 计数、判定、写回放在同一临界区：并发失败请求各自读同一个 cnt 再写回
        # cnt+1 会互相覆盖，5 次上限会被绕过。
        with _fails_lock:
            cnt, t0 = _fails.get(ip, (0, time.time()))
            if time.time() - t0 > 300:
                cnt, t0 = 0, time.time()
            if cnt >= 5:
                return self._send(429, "尝试过多，请稍后再试", "text/plain; charset=utf-8")
            if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                _fails.pop(ip, None)
                ck = f"{COOKIE}={make_cookie()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_TTL}"
                return self._send(302, b"", headers={"Location": "/", "Set-Cookie": ck})
            _fails[ip] = (cnt + 1, t0)
        self._send(401, LOGIN_HTML.replace("%ERR%", '<div class="e">密码错误</div>'), "text/html; charset=utf-8")

    def _generate(self):
        session = None
        raw = self._body()
        if raw:
            try:
                session = (json.loads(raw) or {}).get("session")
            except Exception:
                return self._send(400, {"error": "body 不是合法 JSON"})
        if not _gen_lock.acquire(blocking=False):
            # 后台自检线程可能正在为同一条新消息跑生成。两条 generate 并发会给
            # suggestions.jsonl 各追加一条近重复记录，还白烧一次模型额度。
            return self._send(429, {"error": "已有生成任务在跑，稍后再试"})
        try:
            before = len(read_jsonl(SUGGESTIONS))
            cmd = [sys.executable, str(GENERATE)]
            if session:
                cmd += ["--session", str(session)]
            # 手动点「生成」的语义是「我知道生成过，但我要重来」，所以带 --force；
            # 自检和 watch 走「指纹没变就别重复烧」，它们不带这个参数。
            cmd += ["--force"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=GEN_TIMEOUT, cwd=str(ROOT))
            except subprocess.TimeoutExpired:
                return self._send(502, {"error": f"生成超时（{GEN_TIMEOUT:.0f} 秒）"})
            except Exception as e:
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            if r.returncode != 0:
                if r.returncode == 75:      # generate.EXIT_BUSY：另一处正在生成
                    return self._send(429, {"error": "已有生成任务在跑，稍后再试"})
                return self._send(502, {"error": (r.stderr or r.stdout or f"退出码 {r.returncode}").strip()[-2000:]})
            recs = read_jsonl(SUGGESTIONS)
            if len(recs) <= before:
                return self._send(502, {"error": "generate.py 成功退出但没有写出新建议"})
            self._send(200, recs[-1])
        finally:
            _gen_lock.release()


    def _setup(self):
        """首次配置：挑厂商 → 填 Key/模型 → 服务端真连一次 → 落盘。

        dry_run / list_only 只测不写，给「测试连接」「拉取模型列表」两个按钮用。
        Key 只在请求体里进、只在落盘时写出，不回显、不写日志。
        """
        try:
            req = json.loads(self._body() or b"{}") or {}
        except Exception:
            return self._send(400, {"error": "body 不是合法 JSON"})
        if not isinstance(req, dict):
            return self._send(400, {"error": "body 得是 JSON 对象"})

        preset = providers_cn.by_id(str(req.get("provider_id") or "")) or {}
        base = str(req.get("base_url") or preset.get("base_url") or "").strip().rstrip("/")
        model = str(req.get("model") or "").strip()
        key = str(req.get("api_key") or "").strip()
        list_only = bool(req.get("list_only"))

        if not re.match(r"^https?://[^\s/]+", base):
            return self._send(400, {"error": "接口地址要以 http:// 或 https:// 开头"})
        if key and not re.match(r"^[\x21-\x7e]+$", key):
            # 中文/空格/换行会让 HTTP 头编码直接抛异常，看上去像「网络不通」——
            # 那是最误导人的报错，所以在发请求前就拦掉
            return self._send(400, {"error": "API Key 里不能有空格、换行或中文（是不是粘多了）"})
        saved = saved_provider() or {}
        if not key:
            # 只改模型名/地址、Key 留空时沿用已存的那把；地址变了就必须重填
            same_base = not str(req.get("base_url") or "").strip() or saved.get("base_url") == base
            if saved.get("api_key") and same_base:
                key = saved["api_key"]
            else:
                return self._send(400, {"error": "API Key 没填"})
        if not list_only and not model:
            return self._send(400, {"error": "模型名没填（可以点「拉取模型列表」挑一个）"})

        try:
            ok, models, detail = probe_endpoint(base, key, "" if list_only else model)
        except Exception as e:
            return self._send(502, {"error": "测试连接时出错：%s: %s" % (type(e).__name__, e)})
        models = models[:300]
        if req.get("dry_run") or list_only:
            return self._send(200 if ok else 502, {"ok": ok, "detail": detail, "models": models})
        if not ok:
            return self._send(502, {"ok": False, "detail": detail, "models": models,
                                    "error": "没连上，所以没保存：" + detail})
        try:
            save_llm_cfg(base, key, model)
        except Exception as e:
            return self._send(500, {"error": "写配置失败：%s: %s" % (type(e).__name__, e)})
        print(f"[panel] 已保存模型接口：{base} / {model}（Key 不写日志）", flush=True)
        self._send(200, {"ok": True, "detail": detail, "models": models, "state": setup_state()})

    def _fill(self):
        """把第 index 条候选填进虚机微信输入框（只填，不发送）"""
        raw = self._body()
        try:
            idx = int((json.loads(raw or b"{}") or {}).get("index", 0))
        except Exception:
            return self._send(400, {"error": "index 不是数字"})
        cands = state().get("suggestions") or []
        if not cands:
            return self._send(409, {"error": "当前没有候选回复，先生成"})
        if not 0 <= idx < len(cands):
            return self._send(400, {"error": "index 超出范围（0-%d）" % (len(cands) - 1)})
        text = (cands[idx].get("text") or "").strip()
        if not text:
            return self._send(400, {"error": "这条候选是空的"})
        if not fill_enabled():
            return self._send(403, {"error": "填入已关闭：目前只给建议，不替你回"})
        if not _fill_lock.acquire(blocking=False):
            return self._send(409, {"error": "上一次填入还没结束"})
        try:
            dt = fill_vm.fill(text, log=lambda m: print("[fill]", m, flush=True))
        except Exception as e:
            return self._send(502, {"error": "%s: %s" % (type(e).__name__, e)})
        finally:
            _fill_lock.release()
        self._send(200, {"ok": True, "index": idx, "seconds": round(dt, 1), "text": text})


def main():
    global PASSWORD, SECRET
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址，默认只监听本机（面版是明文 http）")
    ap.add_argument("--lan", action="store_true",
                    help="绑到 0.0.0.0 让局域网能访问；聊天记录和模型 Key 都是明文，自己权衡")
    ap.add_argument("--check-interval", type=float, default=30.0,
                    help="后台自检间隔（秒），0 表示不起自检")
    a = ap.parse_args()
    host = "0.0.0.0" if a.lan else a.host
    PASSWORD, SECRET = load_creds()
    if a.check_interval > 0:
        threading.Thread(target=checker_loop, args=(a.check_interval,), daemon=True).start()
        print(f"[panel] 后台自检已启动，每 {a.check_interval:g}s 看一次新聊天记录"
              f"（关掉：HERMES_PANEL_NO_CHECK=1 或放 store/.no_check）", flush=True)
    srv = ThreadingHTTPServer((host, a.port), H)
    print(f"[panel] 监听 {host}:{a.port}", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("[panel] ⚠️ 监听在非本机地址：面板走明文 http，聊天记录和模型 Key 都会"
              "在局域网里裸奔（家里内网可接受，别往公网映射）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
