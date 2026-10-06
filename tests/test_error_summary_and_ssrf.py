#!/usr/bin/env python3
"""错误摘要（退出码分档）+ base_url 禁内网（可放行、不跟重定向）的自测

用法：python3 tests/test_error_summary_and_ssrf.py
只用标准库 + 临时目录 + 两个本地假服务，不碰 store/ 真实数据。
"""
import http.cookiejar
import http.server
import importlib.util
import json
import os
import pathlib
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
ok_n = fail_n = 0
MARKER = "SECRET_CHAT_TEXT_别回浏览器"


def check(name, cond, detail=''):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f'  [ok]   {name}' + (f'  {detail}' if detail else ''))
    else:
        fail_n += 1
        print(f'  [FAIL] {name}  {detail}')


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


class _FakeProvider(http.server.BaseHTTPRequestHandler):
    """假 provider：/v1/chat/completions 回 200，但 content 是一句带标记的聊天原文（不是 JSON）"""
    hits = 0
    mode = "garbage"

    def log_message(self, *a):
        pass

    def do_POST(self):
        _FakeProvider.hits += 1
        n = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(n)
        body = json.dumps({"choices": [{"message": {"content": MARKER + " 在忙吗"}}]},
                          ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Redirector(http.server.BaseHTTPRequestHandler):
    """外网地址很坏：302 指到内网，看会不会跟着走"""
    target = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header('Location', self.target)
        self.send_header('Content-Length', '0')
        self.end_headers()


class _Sink(http.server.BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Sink.hits += 1
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')


def serve(handler):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def wait_health(base, tries=40):
    for _ in range(tries):
        try:
            urllib.request.urlopen(base + '/api/health', timeout=2).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def login(base, store):
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    pw = (store / '.panel_password').read_text().strip()
    op.open(urllib.request.Request(base + '/login', data=urllib.parse.urlencode({'password': pw}).encode()), timeout=10)
    return op


TMP = pathlib.Path(tempfile.mkdtemp(prefix='wxreply-err-'))
# 加载 server.py 之前就把 store 指到临时目录：safe_check()/write_check() 只碰它
os.environ['WXREPLY_STORE'] = str(TMP / 'store')

# ---------------------------------------------------------------- 造数据
MSGS = TMP / 'store' / 'messages.jsonl'
MSGS.parent.mkdir(parents=True, exist_ok=True)
MSGS.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in [
    {"ts_utc": "2026-10-06T01:02:00Z", "session": "小明", "sender": "them", "text": "在忙吗", "fp": "fA1"},
    {"ts_utc": "2026-10-06T01:03:00Z", "session": "小明", "sender": "me", "text": "刚下课", "fp": "fA2"},
]), encoding='utf-8')
REPLY = ROOT / 'prompts' / 'reply_system.md'
assert REPLY.exists()

GEN = str(ROOT / 'orchestrator' / 'generate.py')


# 注意：CLI 用例走 WXREPLY_CONFIG（只认这一份），否则 load_providers 会顺着候选表
# 回落到 /opt/data/config.yaml 里真实的 provider —— 测试会真的去调模型（还可能是通的）
def stub_cfg(path, base_url=None, provider='custom:stub'):
    d = {"model": {"default": "probe-model", "provider": provider}}
    if base_url:
        d["custom_providers"] = [{"name": provider.split(':', 1)[1], "base_url": base_url, "api_key": "sk-test"}]
    path.write_text(json.dumps(d), encoding='utf-8')
    return path


def run_gen(env_extra, args=()):
    # WXREPLY_STORE 一起给：CLI 的 .gen.lock 也落在临时目录，不去跟线上那个抢
    env = dict(os.environ, WXREPLY_BUDGET='20', WXREPLY_STORE=str(TMP / 'store'), **env_extra)
    return subprocess.run([PY, GEN, '--messages', str(MSGS), '--out', str(TMP / 'sug.jsonl'), *args],
                          capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)


print('\n=== 1. 退出码分档（真跑 CLI）===')
# 5 = 没有消息
cfg5 = stub_cfg(TMP / 'c5.yaml', 'http://127.0.0.1:9/v1')
r = run_gen({'WXREPLY_CONFIG': str(cfg5)}, ['--session', '查无此人'])
check('会话没消息 → 5', r.returncode == 5, f'rc={r.returncode}')

# 2 = 一个能用的 provider 都没配出来
cfg2 = TMP / 'c2.yaml'
cfg2.write_text('model:\n  default: m\n  provider: custom:nope\n', encoding='utf-8')
r = run_gen({'WXREPLY_CONFIG': str(cfg2)})
check('没配出 provider → 2', r.returncode == 2, f'rc={r.returncode}')

# 3 = provider 都失败（端口没人听）
cfg3 = stub_cfg(TMP / 'c3.yaml', 'http://127.0.0.1:9/v1')
r = run_gen({'WXREPLY_CONFIG': str(cfg3)})
check('provider 请求失败 → 3', r.returncode == 3, f'rc={r.returncode}')
check('  stderr 里带真实原因（给日志用）', 'ProviderError' in r.stderr or 'HTTP' in r.stderr or 'Error' in r.stderr)

# 4 = 200 但内容不合规（假 provider 回一句聊天原文）
fake_srv, fake_port = serve(_FakeProvider)
cfg4 = stub_cfg(TMP / 'c4.yaml', f'http://127.0.0.1:{fake_port}/v1')
r = run_gen({'WXREPLY_CONFIG': str(cfg4)})
check('模型输出不合规 → 4', r.returncode == 4, f'rc={r.returncode}')
check('  stderr 里确实有模型原文（所以才不能回浏览器）', MARKER in r.stderr)

print('\n=== 2. base_url 禁内网 ===')
srv_mod = load(ROOT / 'web' / 'server.py', 'srv_ssrf')
for bad, why in [('http://127.0.0.1:11434/v1', '回环'),
                 ('http://192.168.3.50:8000/v1', '私网'),
                 ('http://10.0.2.15:1234/v1', '私网'),
                 ('http://100.64.0.1/v1', 'CGNAT（is_private 不管）'),
                 ('http://169.254.1.1/v1', '链路本地'),
                 ('http://localhost:11434/v1', 'localhost'),
                 ('http://[::1]:11434/v1', 'IPv6 回环')]:
    try:
        srv_mod.check_base_url(bad)
        check(f'拒绝 {why}', False, bad)
    except ValueError as e:
        check(f'拒绝 {why}', '内网' in str(e), str(e)[:40])
try:
    srv_mod.check_base_url('http://1.1.1.1/v1')
    check('公网地址放行', True)
except ValueError as e:
    check('公网地址放行', False, str(e))
try:
    srv_mod.check_base_url('ftp://example.com/x')
    check('非 http(s) 拒绝', False)
except ValueError as e:
    check('非 http(s) 拒绝', True)

(TMP / 'store' / '.allow_private_base').write_text('', encoding='utf-8')
try:
    srv_mod.check_base_url('http://192.168.3.50:8000/v1')
    check('标记文件放行本地模型', True)
except ValueError as e:
    check('标记文件放行本地模型', False, str(e))
os.environ['WXREPLY_ALLOW_PRIVATE_BASE'] = '1'
(TMP / 'store' / '.allow_private_base').unlink()
try:
    srv_mod.check_base_url('http://127.0.0.1:11434/v1')
    check('环境变量放行本地模型', True)
except ValueError as e:
    check('环境变量放行本地模型', False, str(e))
del os.environ['WXREPLY_ALLOW_PRIVATE_BASE']

print('\n=== 3. 重定向要掐掉（否则 302 到内网照样打进去）===')
sink_srv, sink_port = serve(_Sink)
_Redirector.target = f'http://127.0.0.1:{sink_port}/v1/models'
redir_srv, redir_port = serve(_Redirector)
_sink_hits_before = _Sink.hits
status, body = srv_mod._http_json(f'http://127.0.0.1:{redir_port}/v1/models', 'sk-x', timeout=8)
check('302 被当成失败返回（不跟）', status == 302, f'status={status}')
check('内网跳转目标一次都没被打到', _Sink.hits == _sink_hits_before, f'hits={_Sink.hits}')

print('\n=== 4. 面板端到端：错误摘要 + 不泄漏模型原文 ===')
def panel_case(name, base_url, want_error, log_want):
    store = TMP / 'store'
    for f in (store / 'suggestions.jsonl',):
        if f.exists():
            f.unlink()
    cfg = stub_cfg(TMP / 'cp.yaml', base_url)
    port = 8931
    proc = subprocess.Popen([PY, str(ROOT / 'web' / 'server.py'), '--port', str(port), '--check-interval', '0'],
                            env=dict(os.environ, WXREPLY_STORE=str(store), WXREPLY_LLM_CONFIG=str(cfg),
                                     WXREPLY_BUDGET='20'),
                            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        b = f'http://127.0.0.1:{port}'
        if not wait_health(b):
            check(f'{name}：面板起来', False)
            return
        op = login(b, store)
        req = urllib.request.Request(b + '/api/generate', data=b'{}',
                                    headers={'Content-Type': 'application/json'})
        try:
            r = op.open(req, timeout=120)
            code, text = r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read().decode('utf-8', 'replace')
        check(f'{name}：502 + 摘要「{want_error}」', code == 502 and want_error in text, f'{code} {text[:90]}')
        check(f'{name}：stderr 原文没回浏览器', MARKER not in text and '  - ' not in text,
              '含原始 stderr' if ('  - ' in text or MARKER in text) else '不含原始 stderr')
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            out = proc.communicate(timeout=6)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        check(f'{name}：真实原因进了服务端日志（{log_want}）', log_want in (out or ''),
              f'日志 {len(out or "")} 字')


panel_case('provider 请求失败', 'http://127.0.0.1:9/v1', '模型接口请求失败（Key、额度或网络）', '手动生成失败')
panel_case('模型输出不合规', f'http://127.0.0.1:{fake_port}/v1', '模型返回的格式不对，已可重试', MARKER)

print('\n=== 5. checker.json 里旧的长报错不再回浏览器 ===')
store = TMP / 'store'
(store / 'checker.json').write_text(json.dumps(
    {"last_result": "error", "error": "所有 provider 都失败了:\n" + MARKER * 30}, ensure_ascii=False), encoding='utf-8')
c = srv_mod.safe_check()
check('长 error 被替换成一句人话', c['error'] == '生成失败，详情见服务端日志' and MARKER not in json.dumps(c, ensure_ascii=False),
      str(c['error'])[:40])
srv_mod.write_check(error='当前会话没有消息')
check('短 error 原样保留', srv_mod.safe_check()['error'] == '当前会话没有消息')
check('改的是临时 store，没碰真实数据', srv_mod.STORE == TMP / 'store', str(srv_mod.STORE))

print(f'\n合计：通过 {ok_n}，失败 {fail_n}')
sys.exit(1 if fail_n else 0)
