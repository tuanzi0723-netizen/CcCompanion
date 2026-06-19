#!/usr/bin/env python3
"""CcCompanion Stop hook — 把 claude 一轮回复按段落拆成多条, 逐条 POST 给
apns-server /chat/append, iOS 端就会一条条收到 (而不是一个大气泡)。

- 段落边界 = 空行; 代码块 (``` 包裹) 整体保留不拆。
- stdin 收 Claude Code 传的 JSON (last_assistant_message / transcript_path)。
- 鉴权 token: 环境变量 CCC_AUTH_TOKEN, 否则读 ~/.ots/secret。
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import time
import urllib.request

SERVER = os.environ.get("CCC_SERVER_URL", "http://127.0.0.1:8795")
TOKEN = os.environ.get("CCC_AUTH_TOKEN", "")
if not TOKEN:
    secret_file = pathlib.Path.home() / ".ots" / "secret"
    if secret_file.exists():
        TOKEN = secret_file.read_text().strip()

LOG_PATH = "/tmp/ccc_stop_hook.log"
GAP_SECONDS = 0.7  # 逐条冒出来的间隔


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def read_transcript_tail(transcript_path: str) -> str:
    """fallback: 倒扫 transcript JSONL 抓自上次 user 以来的 assistant 文本。"""
    # 等 transcript flush 稳定 (最多 ~3s)
    last_size = -1
    stable = 0
    for _ in range(10):
        time.sleep(0.3)
        try:
            size = os.path.getsize(transcript_path)
        except OSError:
            size = 0
        if size == last_size:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_size = size

    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""

    collected: list[str] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "user":
            break
        if t == "assistant":
            content = obj.get("message", {}).get("content", [])
            parts = [
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
            ]
            if parts:
                collected.append("\n".join(parts))
    collected.reverse()
    return "\n\n".join(collected).strip()


def split_paragraphs(text: str) -> list[str]:
    """按空行切段; ``` 围栏代码块整体保留不拆。"""
    chunks: list[str] = []
    cur: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            cur.append(line)
            continue
        if not in_code and line.strip() == "":
            if any(x.strip() for x in cur):
                chunks.append("\n".join(cur).strip())
            cur = []
        else:
            cur.append(line)
    if any(x.strip() for x in cur):
        chunks.append("\n".join(cur).strip())
    return [c for c in chunks if c.strip()]


def post_chunk(text: str) -> int:
    payload = json.dumps({
        "role": "assistant",
        "text": text,
        "source": "ccc-stop-hook",
        "ts": datetime.datetime.now(datetime.timezone.utc)
              .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }).encode("utf-8")
    req = urllib.request.Request(f"{SERVER}/chat/append", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Auth-Token", TOKEN)
    # 显式禁用代理 (本机 loopback, 不走 TUN/proxy)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=8) as resp:
            return resp.status
    except Exception as e:
        log(f"POST failed: {e}")
        return 0


def main() -> None:
    raw = sys.stdin.read() or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    text = (data.get("last_assistant_message") or "").strip()
    if not text:
        tp = data.get("transcript_path") or ""
        if tp and os.path.exists(tp):
            text = read_transcript_tail(tp)

    if not text:
        log("empty assistant text — skip")
        return

    chunks = split_paragraphs(text) or [text]

    ok = 0
    for i, chunk in enumerate(chunks):
        if post_chunk(chunk) == 200:
            ok += 1
        if i < len(chunks) - 1:
            time.sleep(GAP_SECONDS)
    log(f"posted {ok}/{len(chunks)} chunks ok")


if __name__ == "__main__":
    main()
