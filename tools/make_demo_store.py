#!/usr/bin/env python3
"""造一份合成的演示数据（不碰真实聊天记录）。

用途：
  * 面板截图 / README 配图
  * 第一次跑起来想看界面长什么样

用法:
  python3 tools/make_demo_store.py --out demo
  # 然后把 demo/ 里的两个 jsonl 拷进你实际跑的 store/（或在副本项目里跑面板）

生成的是固定内容的假对话，字段结构和采集器写出来的一致（ts_utc / session /
sender / text / time_hint / fp），所以生成器、面板、自检都能直接吃。
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone


def fp(session, text, occ=1):
    raw = "\x1f".join([session, text, str(occ)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


SESSION = "林同学"
DIALOG = [
    ("them", "下周的展示你来吗", "14:02"),
    ("me", "来的，我准备一下", "14:05"),
    ("them", "那正好，我也有事说", "14:06"),
    ("me", "什么事", "14:07"),
    ("them", "先不剧透，到时候你就知道了", "14:08"),
    ("them", "对了，你上次说的那个文档还在吗", "14:20"),
    ("me", "在的，我找找", "14:22"),
    ("them", "不急，明天给我也行", "14:23"),
    ("me", "找到了，怎么发你", "14:31"),
    ("them", "微信传我吧", "14:32"),
    ("me", "好", "14:33"),
    ("them", "你还在上课吗", "20:11"),
    ("me", "刚下课", "20:12"),
    ("them", "那晚上有空吗", "20:13"),
    ("them", "想找你帮个忙", "20:13"),
    ("me", "怎么了", "20:14"),
]

CANDIDATES = [
    {"text": "有空呀，什么事", "score": 86, "style": "自然"},
    {"text": "刚下课，你说", "score": 71, "style": "简短"},
    {"text": "今晚有点事，明天行吗", "score": 52, "style": "留余地"},
]


def main():
    ap = argparse.ArgumentParser(description="生成演示用的假聊天记录 + 一条示例建议")
    ap.add_argument("--out", default="demo", help="输出目录，默认 demo/")
    ap.add_argument("--session", default=SESSION)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    t0 = datetime(2026, 1, 5, 14, 2, tzinfo=timezone.utc)
    msgs = []
    for i, (who, text, hint) in enumerate(DIALOG):
        msgs.append({
            "ts_utc": (t0 + timedelta(minutes=i * 7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": a.session,
            "sender": who,
            "text": text,
            "time_hint": hint,
            "fp": fp(a.session, text),
        })
    with open(os.path.join(a.out, "messages.jsonl"), "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    last_them = [m for m in msgs if m["sender"] == "them"][-1]
    rec = {
        "ts_utc": last_them["ts_utc"],
        "session": a.session,
        "provider": "demo",
        "model": "demo",
        "strategy": "先接住问题，别急着给方案",
        "intent": "对方有具体请求，先问清是什么事",
        "need": "被认真对待",
        "tension": 3,
        "candidates": CANDIDATES,
        "source_msg_count": len(msgs),
        "source_last_fp": last_them["fp"],
    }
    with open(os.path.join(a.out, "suggestions.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"已生成 {a.out}/messages.jsonl（{len(msgs)} 条）和 {a.out}/suggestions.jsonl（1 条建议）")


if __name__ == "__main__":
    main()
