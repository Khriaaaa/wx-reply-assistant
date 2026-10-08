#!/usr/bin/env python3
"""AI 聊天回复助手 —— 一键起停（采集 + 自动生成 + 面板）

用法:
  python3 assistant.py up   [--port 8801] [--no-collector] [--no-watch]
  python3 assistant.py down
  python3 assistant.py status

up 起三个东西：
  1. 采集循环（虚机 session 1，读微信当前聊天 -> store/messages.jsonl）
  2. 自动生成（orchestrator/watch.py，来了新消息就调 LLM 出 3 条候选）
  3. 面板（web/server.py，局域网可开，带密码）
down 全停，包括虚机里的采集进程。
只读采集，不发送、不填入、不碰输入框。
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Windows 上 stdout/stderr 被重定向到文件（本文件起子进程时就是这样）会按本地代码页
# 编码，碰到 ⚠️ 这类字符直接 UnicodeEncodeError 崩在启动路径上。统一压成 UTF-8、
# 编不出来的替换掉 —— 宁可少几个符号，也不能让面板起不来。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

IS_WIN = os.name == "nt"
PY = sys.executable
ROOT = Path(__file__).resolve().parent
STORE = Path(os.environ.get("WXREPLY_STORE", str(ROOT / "store")))
PIDS = STORE / ".assistant_pids.json"
COLLECTOR = ROOT / "collector" / "wx_collector.py"
WATCH = ROOT / "orchestrator" / "watch.py"
SERVER = ROOT / "web" / "server.py"


def load_pids():
    if PIDS.exists():
        try:
            return json.loads(PIDS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def alive(pid):
    pid = int(pid)
    if IS_WIN:
        # Windows 没有 os.kill(pid, 0) 这套，用 OpenProcess 探活
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def kill_tree(pid):
    """连子进程一起收掉：Windows 用 taskkill /T，POSIX 用进程组。"""
    pid = int(pid)
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=30)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        os.kill(pid, signal.SIGTERM)


def spawn(name, cmd, pids, logdir=None):
    log = (logdir or (STORE / "logs"))
    log.mkdir(parents=True, exist_ok=True)
    f = open(log / f"{name}.log", "ab", buffering=0)
    try:
        # 日志里有聊天片段（generate 的 stderr 原文、模型返回），别让同机别人看
        os.chmod(log / f"{name}.log", 0o600)
    except OSError:
        pass
    # 脱离当前进程：Windows 用 DETACHED_PROCESS，POSIX 用 start_new_session
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
          if IS_WIN else {"start_new_session": True})
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                         cwd=str(ROOT), **kw)
    pids[name] = p.pid
    print(f"  {name}: pid {p.pid}  日志 {log / (name + '.log')}")
    return p


def lan_ip():
    """探一个本机局域网 IP（给手机开面板用）。探不到返回空串，绝不抛。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))      # 不发包，只为让内核挑出口地址
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def entry_url(port, ttl=180):
    """拼一个带进门票的面板地址：打开就是 UI，不用手打密码。

    票签在 store/.panel_secret 上，和面板自己校验用的是同一把密钥，服务端只认
    本机来源。密钥文件还没生成（面板从没起过）就退回不带票的地址。
    """
    try:
        # .strip()：密钥文件末尾带换行，面板读的时候也 strip，不然签名对不上
        sec = (STORE / ".panel_secret").read_bytes().strip()
    except Exception:
        return f"http://127.0.0.1:{port}/"
    exp = str(int(time.time()) + ttl)
    sig = hmac.new(sec, ("entry." + exp).encode(), hashlib.sha256).hexdigest()
    return f"http://127.0.0.1:{port}/?k={exp}.{sig}"


def find_browser():
    """找默认浏览器的 exe：先问注册表，再试常见安装位置。找不到返回 None。

    为什么不用 os.startfile 了事：SYSTEM / 没有用户配置的会话里 ShellExecute
    会静默什么都不做（不报错、也不弹窗），直接拉浏览器反而稳。
    """
    if not IS_WIN:
        return None
    import winreg
    try:
        sub = (r"Software\Microsoft\Windows\Shell\Associations"
               r"\UrlAssociations\http\UserChoice")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
            prog = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            prog + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        m = re.match(r'\s*"([^"]+)"', cmd) or re.match(r"\s*(\S+)", cmd)
        if m and Path(m.group(1)).exists():
            return m.group(1)
    except Exception:
        pass
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return p
    return None


def open_browser(url):
    """打开面板。打不开返回 False，由调用方把地址兜出来。"""
    if IS_WIN:
        exe = find_browser()
        if exe:
            try:
                subprocess.Popen([exe, url], close_fds=True)
                return True
            except Exception:
                pass
    try:
        if IS_WIN:
            os.startfile(url)          # noqa: S606 —— 兜底：交给系统默认程序
        else:
            # 非 Windows（NAS 那套）没有浏览器就返回 False，让调用方把地址打出来
            import webbrowser
            return bool(webbrowser.open(url))
        return True
    except Exception:
        return False


def cmd_up(a):
    STORE.mkdir(parents=True, exist_ok=True)
    pids = {k: v for k, v in load_pids().items() if alive(v)}
    if pids:
        print("已在运行:", ", ".join(f"{k}({v})" for k, v in pids.items()))
        print("先 down 再 up，或直接用 status 看状态")
        return 1

    print("起服务:")
    if not a.no_collector:
        r = subprocess.run([PY, str(COLLECTOR), "start"], capture_output=True,
                           text=True, timeout=200, cwd=str(ROOT))
        out = (r.stdout or "") + (r.stderr or "")
        tail = [x for x in out.strip().splitlines() if x.strip()]
        print("  采集:", tail[-1] if tail else "?")
        if r.returncode != 0:
            print("  ⚠️ 采集启动失败，完整输出:")
            print("\n".join(tail[-12:]))
            return 1

    if not a.no_watch:
        spawn("watch", [PY, str(WATCH)], pids)

    if not a.no_collector:
        spawn("sync", [PY, str(COLLECTOR), "sync", "--quiet"], pids)

    panel_args = [PY, str(SERVER), "--port", str(a.port)]
    if a.lan:
        panel_args.append("--lan")
    spawn("panel", panel_args, pids)

    time.sleep(3)
    up_ok = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/health", timeout=5) as r:
            up_ok = r.status == 200
            print("  面板健康检查:", r.status)
    except Exception as e:
        print("  面板健康检查失败:", e)

    PIDS.write_text(json.dumps(pids, indent=2), encoding="utf-8")

    # 直接把浏览器开在面板里（带进门票，不用手打密码）。开不起来才把地址兜出来。
    url = entry_url(a.port) if up_ok else f"http://127.0.0.1:{a.port}/"
    if a.no_open:
        # 桌面壳（Electron）自己开窗口，这里只报状态，别再去弹一个系统浏览器
        print("\n面板就绪" if up_ok else "\n面板没起来")
    elif up_ok and open_browser(url):
        print("\n面板已打开")
    else:
        print("\n面板地址", url)
    if a.lan:
        _ip = lan_ip()
        if _ip:
            print("局域网地址 http://%s:%d（手机连同一个 Wi-Fi 就能开，这个要密码）" % (_ip, a.port))
        else:
            print("（没探到局域网 IP，自己 ipconfig 看一眼）")
    return 0


def cmd_down(a):
    pids = load_pids()
    for name, pid in pids.items():
        if alive(pid):
            try:
                kill_tree(pid)
            except Exception as e:
                print(f"  {name}({pid}) 停止失败: {e}")
                continue
            print(f"  {name}({pid}) 已停")
    time.sleep(2)
    r = subprocess.run([PY, str(COLLECTOR), "stop"], capture_output=True,
                       text=True, timeout=200, cwd=str(ROOT))
    print("  采集:", (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "?")
    if PIDS.exists():
        PIDS.unlink()
    return 0


def cmd_status(a):
    pids = load_pids()
    if not pids:
        print("assistant 没在跑")
    for name, pid in pids.items():
        print(f"  {name}: pid {pid} {'在跑' if alive(pid) else '已死'}")
    hb = STORE / "cache" / "heartbeat.txt"
    if hb.exists():
        age = time.time() - hb.stat().st_mtime
        print(f"  采集心跳(本地缓存): {hb.read_text(encoding='utf-8').strip()} ({age:.0f}s 前)")
    else:
        print("  采集心跳(本地缓存): 无")
    try:
        r = subprocess.run([PY, str(COLLECTOR), "status", "--tail", "3"], capture_output=True,
                           text=True, timeout=120, cwd=str(ROOT))
        for line in (r.stdout or "").splitlines()[:4]:
            print("  " + line)
    except Exception as e:
        print("  虚机采集状态取不到:", e)
    w = STORE / "watcher.json"
    if w.exists():
        d = json.loads(w.read_text(encoding="utf-8"))
        print(f"  最近生成: {d.get('last_trigger')} 结果={d.get('last_result')} 会话={d.get('last_session')}")
    m = STORE / "messages.jsonl"
    s = STORE / "suggestions.jsonl"
    print(f"  消息 {sum(1 for _ in m.open(encoding='utf-8')) if m.exists() else 0} 条, "
          f"候选 {sum(1 for _ in s.open(encoding='utf-8')) if s.exists() else 0} 组")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/health", timeout=5) as r:
            print("  面板: 在跑")
    except Exception:
        print("  面板: 没响应")
    return 0


def main():
    ap = argparse.ArgumentParser(description="微信回复助手一键起停")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="起采集 + 自动生成 + 面板")
    up.add_argument("--port", type=int, default=8801)
    up.add_argument("--lan", action="store_true",
                    help="面板也监听局域网（手机访问用）；默认只听本机，避免防火墙弹窗")
    up.add_argument("--no-collector", action="store_true")
    up.add_argument("--no-open", action="store_true",
                    help="不自动开浏览器（桌面壳自己开窗口时用）")
    up.add_argument("--no-watch", action="store_true")
    ur = sub.add_parser("url", help="打印带进门票的面板地址（图省事时手动开）")
    ur.add_argument("--port", type=int, default=8801)
    dn = sub.add_parser("down", help="全停")
    st = sub.add_parser("status", help="看状态")
    st.add_argument("--port", type=int, default=8801)
    a = ap.parse_args()
    if a.cmd == "up":
        sys.exit(cmd_up(a))
    if a.cmd == "url":
        print(entry_url(a.port))
        sys.exit(0)
    if a.cmd == "down":
        sys.exit(cmd_down(a))
    sys.exit(cmd_status(a))


if __name__ == "__main__":
    main()
