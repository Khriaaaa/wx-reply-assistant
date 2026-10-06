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
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IS_WIN = os.name == "nt"
PY = sys.executable
ROOT = Path(__file__).resolve().parent
STORE = ROOT / "store"
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

    spawn("panel", [PY, str(SERVER), "--port", str(a.port), "--lan"], pids)

    time.sleep(3)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/health", timeout=5) as r:
            print("  面板健康检查:", r.status, r.read().decode()[:40])
    except Exception as e:
        print("  面板健康检查失败:", e)

    PIDS.write_text(json.dumps(pids, indent=2), encoding="utf-8")
    print(f"\n面板地址 http://<NAS_IP>:{a.port}   密码在 store/.panel_password")
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
    up.add_argument("--no-collector", action="store_true")
    up.add_argument("--no-watch", action="store_true")
    dn = sub.add_parser("down", help="全停")
    st = sub.add_parser("status", help="看状态")
    st.add_argument("--port", type=int, default=8801)
    a = ap.parse_args()
    if a.cmd == "up":
        sys.exit(cmd_up(a))
    if a.cmd == "down":
        sys.exit(cmd_down(a))
    sys.exit(cmd_status(a))


if __name__ == "__main__":
    main()
