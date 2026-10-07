#!/usr/bin/env python3
"""手动补上下文（/api/msg）的自测：空 store、正常、坏输入、以及手动行真的被生成端吃进去。

用法：python3 tests/test_manual_context.py
只用标准库 + 临时目录，不碰 store/ 里的真实数据，也不调模型。
"""
import http.cookiejar
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
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


def wj(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')


def rj(p):
    return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()] if p.exists() else []


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


def post_json(op, base, path, obj):
    req = urllib.request.Request(base + path, method='POST',
                                 data=json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    try:
        r = op.open(req, timeout=10)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


TMP = pathlib.Path(tempfile.mkdtemp(prefix='wxreply-manual-'))
ST = TMP / 'store' / 'fresh'
MSGS = ST / 'messages.jsonl'
SUGS = ST / 'suggestions.jsonl'
ST.mkdir(parents=True, exist_ok=True)
(MSGS.touch(), SUGS.touch())

port = 8921
proc = subprocess.Popen([PY, str(ROOT / 'web' / 'server.py'), '--port', str(port), '--check-interval', '0'],
                        env=dict(os.environ, WXREPLY_STORE=str(ST)), cwd=str(ROOT),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    base = f'http://127.0.0.1:{port}'
    check('面板起来了', wait_health(base))
    op = login(base, ST)

    print('\n=== 1. 全新空 store：第一条手动消息也能进 ===')
    st_code, st = post_json(op, base, '/api/msg', {'sender': 'them', 'text': '在吗，找了你好久'})
    check('空 store 也接受', st_code == 200, f'{st_code} {st}')
    check('给了兜底会话名', (st.get('state') or {}).get('session') == '未命名会话', str(st.get('state', {}).get('session')))
    rows = rj(MSGS)
    check('messages.jsonl 里多了一行', len(rows) == 1, str(len(rows)))
    check('行结构对齐采集器', all(k in rows[0] for k in ('ts_utc', 'session', 'sender', 'text', 'fp')), str(rows[0].keys()))
    check('挂着 manual 标记', rows[0].get('manual') is True)
    check('fp 是带手动前缀的唯一指纹', str(rows[0].get('fp', '')).startswith('h-manual-'))

    print('\n=== 2. 坏输入都拦在入口 ===')
    for name, body, code in [('空文本', {'sender': 'me', 'text': '  '}, 400),
                             ('超长文本', {'sender': 'me', 'text': 'a' * 3000}, 400),
                             ('未知 sender', {'sender': 'other', 'text': 'x'}, 400),
                             ('body 不是对象', ['me', 'x'], 400)]:
        c, _ = post_json(op, base, '/api/msg', body)
        check(f'{name} → {code}', c == code, f'got {c}')
    check('坏输入一个都没写进去', len(rj(MSGS)) == 1, str(len(rj(MSGS))))

    print('\n=== 3. 同一句重发不撞采集器的指纹（当新消息算）===')
    c, _ = post_json(op, base, '/api/msg', {'sender': 'them', 'text': '在吗，找了你好久'})
    check('同文本第二遍也接受', c == 200)
    rows = rj(MSGS)
    check('现在是两行', len(rows) == 2, str(len(rows)))
    check('两行指纹不同', rows[0]['fp'] != rows[1]['fp'])

    print('\n=== 4. 未登录先 401 ===')
    anon = urllib.request.build_opener()
    try:
        urllib.request.urlopen(urllib.request.Request(base + '/api/msg', method='POST',
                                                      data=b'{"text":"x"}'), timeout=10)
        check('未登录拒绝', False)
    except urllib.error.HTTPError as e:
        check('未登录拒绝', e.code == 401, f'code={e.code}')

    print('\n=== 5. 回退链：已有消息时手动行进对了会话 ===')
    wj(MSGS, [
        {"ts_utc": "2026-10-07T01:00:00Z", "session": "B", "sender": "me", "text": "好", "fp": "fB2"},
        {"ts_utc": "2026-10-07T01:02:00Z", "session": "A", "sender": "them", "text": "明天有空吗", "fp": "fA1"},
    ])
    c, st = post_json(op, base, '/api/msg', {'sender': 'them', 'text': '对了还有件事'})
    check('进了最后一条对方消息的会话 A', (st.get('state') or {}).get('session') == 'A', str(st.get('state', {}).get('session')))
    check('那一行的 session 字段也是 A', rj(MSGS)[-1].get('session') == 'A')
    st2 = json.loads(op.open(base + '/api/state', timeout=10).read())
    manual = [m for m in st2['messages'] if m.get('manual')]
    check('面板能展示手动行（带标记）', len(manual) == 1 and manual[0]['text'] == '对了还有件事')
    check('对方最近说 = 手动补的这条', (st2.get('last_other') or {}).get('text') == '对了还有件事')

    print('\n=== 6. 生成端真的吃到手动行（不调模型，看 prompt）===')
    # generate --dry-run 打印完整 prompt：手动行在 transcript 里就说明下轮生成用得上
    r = subprocess.run([PY, str(ROOT / 'orchestrator' / 'generate.py'),
                        '--session', 'A', '--dry-run'],
                       capture_output=True, text=True, env=dict(os.environ, WXREPLY_STORE=str(ST)),
                       cwd=str(ROOT), timeout=60)
    check('dry-run 成功', r.returncode == 0, r.stderr[-200:])
    check('手动行进了 prompt', '对了还有件事' in r.stdout and '对方(A)' in r.stdout, r.stdout[-200:])
finally:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()

shutil.rmtree(TMP, ignore_errors=True)
print(f'\n合计：通过 {ok_n}，失败 {fail_n}')
sys.exit(1 if fail_n else 0)
