#!/usr/bin/env python3
"""面板层后端 v1（只读，仅标准库）

用法:
  python3 web/server.py [--port 8801] [--host 0.0.0.0]
路径相对脚本位置，不依赖 cwd。数据在 ../store/。
密码: ../store/.panel_password（不存在则随机生成，仅首次生成时在日志打印一次）
接口: GET / | /login | /api/state | /api/health ; POST /login | /api/generate | /api/fill
/api/fill 把某条候选填进 NAS 虚机里微信的输入框（只填不发送，见 ../tools/fill_vm.py）。
**默认关闭**：目标就是「只给建议、不替你回」。要开就设环境变量 HERMES_PANEL_ALLOW_FILL=1
或放一个 store/.allow_fill 文件，重启即生效；关闭时接口一律 403。
除 /login 与 /api/health 外均需登录。
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
STORE = ROOT / "store"
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
    两边永远对不上、每 45 秒白烧一次模型；改成「不论谁说的最后一条」之后，
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

    # 会话也跟着「最后一条对方消息」走：不然我在 B 会话发了最后一条、A 会话来了新消息时，
    # 自检会拿 B 去生成（生成的是我已经回过的那边）。
    session = next((m.get("session") for m in reversed(msgs)
                    if m.get("sender") == "them" and m.get("session")), None)
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
        print(f"[panel] 已生成{label}: {val}  (仅此一次显示，之后见 store 下 .panel_password)", flush=True)
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


def read_jsonl(path):
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def state():
    msgs = read_jsonl(MESSAGES)
    recs = read_jsonl(SUGGESTIONS)
    sug = recs[-1] if recs else None
    session = msgs[-1].get("session") if msgs else (sug or {}).get("session")
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

    def do_GET(self):
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
            if path == "/api/fill":
                return self._fill()
            self._send(404, {"error": "not found"})
        except _BodyTooLarge:
            return self._send(413, {"error": "请求体过大（上限 64KB）"})

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
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=GEN_TIMEOUT, cwd=str(ROOT))
            except subprocess.TimeoutExpired:
                return self._send(502, {"error": "生成超时（180 秒）"})
            except Exception as e:
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            if r.returncode != 0:
                return self._send(502, {"error": (r.stderr or r.stdout or f"退出码 {r.returncode}").strip()[-2000:]})
            recs = read_jsonl(SUGGESTIONS)
            if len(recs) <= before:
                return self._send(502, {"error": "generate.py 成功退出但没有写出新建议"})
            self._send(200, recs[-1])
        finally:
            _gen_lock.release()


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
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--check-interval", type=float, default=30.0,
                    help="后台自检间隔（秒），0 表示不起自检")
    a = ap.parse_args()
    PASSWORD, SECRET = load_creds()
    if a.check_interval > 0:
        threading.Thread(target=checker_loop, args=(a.check_interval,), daemon=True).start()
        print(f"[panel] 后台自检已启动，每 {a.check_interval:g}s 看一次新聊天记录"
              f"（关掉：HERMES_PANEL_NO_CHECK=1 或放 store/.no_check）", flush=True)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"[panel] 监听 {a.host}:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
