#!/usr/bin/env python3
"""模型链路测试（/api/setup test_saved）的自测：用本机桩端点冒充 /v1，不碰外网。

用法：python3 tests/test_llm_linktest.py
桩端点起在 127.0.0.1 随机端口，所以面板要挂 WXREPLY_ALLOW_PRIVATE_BASE=1
（这正是它的用途：本机自测时放行内网地址）。
只在临时 store 里落 .llm.yaml，不碰真实数据。
"""
import http.cookiejar
import http.server
import json
import os
import pathlib
import shutil
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


def check(name, cond, detail=''):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f'  [ok]   {name}' + (f'  {detail}' if detail else ''))
    else:
        fail_n += 1
        print(f'  [FAIL] {name}  {detail}')


class StubV1(http.server.BaseHTTPRequestHandler):
    """假 /v1：/models 回一个模型列表；用类属性开关控制死活。"""
    alive = True
    models_ok = True
    key_expected = 'sk-test-1234'

    def do_GET(self):
        if not self.alive:
            self.send_response(500)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.headers.get('Authorization') != 'Bearer ' + self.key_expected:
            body = b'{"error":{"message":"bad key"}}'
            self.send_response(401)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.models_ok:
            self.send_response(500)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = json.dumps({'data': [{'id': 'stub-a'}, {'id': 'stub-b'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Key 错时 probe_endpoint 会落一个 1-token 的 chat/completions 试探，
        # 桩得接住并同样按 Key 判 401，别让 http.server 的 501 抢戏
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        if self.headers.get('Authorization') != 'Bearer ' + self.key_expected:
            body = b'{"error":{"message":"bad key"}}'
            self.send_response(401)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.alive or not self.models_ok:
            self.send_response(500)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = json.dumps({'choices': [{'message': {'content': 'ok'}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), StubV1)
STUB_PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
STUB_BASE = f'http://127.0.0.1:{STUB_PORT}/v1'

TMP = pathlib.Path(tempfile.mkdtemp(prefix='wxreply-linktest-'))
ST = TMP / 'store'
ST.mkdir(parents=True, exist_ok=True)

port = 8924
proc = subprocess.Popen(
    [PY, str(ROOT / 'web' / 'server.py'), '--port', str(port), '--check-interval', '0'],
    env=dict(os.environ, WXREPLY_STORE=str(ST), WXREPLY_ALLOW_PRIVATE_BASE='1'),
    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    base = f'http://127.0.0.1:{port}'
    for _ in range(40):
        try:
            urllib.request.urlopen(base + '/api/health', timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    check('面板起来了', True)

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    pw_file = ST / '.panel_password'

    def post(path, obj):
        req = urllib.request.Request(base + path, method='POST',
                                     data=json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        try:
            r = op.open(req, timeout=15)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}

    # 登录（面板无密码文件时会自动生成一个，读出来用）
    pw = pw_file.read_text().strip() if pw_file.exists() else 'x'
    op.open(urllib.request.Request(base + '/login',
                                   data=urllib.parse.urlencode({'password': pw}).encode()), timeout=10)

    print('\n=== 1. 没保存过配置时 test_saved 要给一句人话 ===')
    c, body = post('/api/setup', {'test_saved': True})
    check('返回 400/502 而不是 tracker 崩掉', c in (400, 502), f'{c} {body}')
    check('报错里说清是没有配置', '没' in (body.get('error') or ''), str(body.get('error')))

    print('\n=== 2. 正常保存后再测：走桩端点 ===')
    c, body = post('/api/setup', {'base_url': STUB_BASE, 'api_key': StubV1.key_expected,
                                  'model': 'stub-a', 'provider_id': 'custom'})
    check('保存本身要过', c == 200, f'{c} {body}')

    c, body = post('/api/setup', {'test_saved': True})
    check('test_saved 200', c == 200, f'{c} {body}')
    check('ok 为真', body.get('ok') is True, str(body))
    check('detail 带模型数', '2 个模型' in (body.get('detail') or ''), str(body.get('detail')))
    check('回的 models 就是列表', body.get('models') == ['stub-a', 'stub-b'], str(body.get('models')))
    check('不回显 Key', 'sk-test' not in json.dumps(body, ensure_ascii=False), '')

    print('\n=== 3. 链路半死时（Key 错）要给真实原因 ===')
    StubV1.key_expected = 'sk-changed'
    c, body = post('/api/setup', {'test_saved': True})
    check('返回 502', c == 502, f'{c} {body}')
    check('ok 为假', body.get('ok') is False, str(body))
    check('detail 是 401 原话', '401' in (body.get('detail') or ''), str(body.get('detail')))
    StubV1.key_expected = StubV1.key_expected  # noqa: PLW0127 只是标注语义

    StubV1.key_expected = 'sk-test-1234'
    StubV1.alive = False
    StubV1.models_ok = False   # 同时封掉 GET 200 兜底，让链路真死透
    c, body = post('/api/setup', {'test_saved': True})
    check('端点整个死掉也报 502', c == 502, f'{c} {body}')
    StubV1.alive = True
    StubV1.models_ok = True

    print('\n=== 4. 另一条现实主义用例：保存时地址变了、Key 留空沿用旧 Key 的路径不破 ===')
    c, body = post('/api/setup', {'model': 'stub-b', 'base_url': STUB_BASE})
    check('只改模型、Key 沿用旧的那把（同地址）', c == 200, f'{c} {body}')
finally:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()
srv.shutdown()

shutil.rmtree(TMP, ignore_errors=True)
print(f'\n合计：通过 {ok_n}，失败 {fail_n}')
sys.exit(1 if fail_n else 0)
