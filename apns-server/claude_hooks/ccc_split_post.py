#!/usr/bin/env python3
"""CcCompanion Stop hook — 把 claude 一轮回复按段落拆成多条逐条 POST，
并（可选）抽取本轮思考摘要上报 /v1/thinking，让 iOS 端在该轮第一条气泡上方
渲染思考卡片。

- 段落边界 = 空行; 代码块 (``` 包裹) 整体保留不拆。
- 思考卡片需 claude 启动带 --thinking-display summarized; 没带则 transcript 里
  没有 thinking 块, 本段静默跳过, 行为不变。
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
GAP_SECONDS = 0.7

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def read_transcript_lines(transcript_path: str) -> list[str]:
    """等 transcript flush 稳定后读取所有行。"""
    last_size, stable = -1, 0
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
            return f.readlines()
    except OSError:
        return []


def assistant_text_from_lines(lines: list[str]) -> str:
    """倒扫抓自上次 user 以来的 assistant text。"""
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
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
            if parts:
                collected.append("\n".join(parts))
    collected.reverse()
    return "\n\n".join(collected).strip()


def thinking_from_lines(lines: list[str]) -> str:
    """倒扫抽本轮 thinking 簇。tool_result 型 user 行跳过; 收到 thinking 后再遇
    真 user 行才停。扫描上限 120 行。"""
    def is_tool_result_user(obj):
        content = obj.get("message", {}).get("content", [])
        return isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        )

    collected: list[str] = []
    scanned = 0
    for line in reversed(lines):
        scanned += 1
        if scanned > 120:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "user":
            if is_tool_result_user(obj):
                continue
            if collected:
                break
            continue
        if t == "assistant":
            content = obj.get("message", {}).get("content", [])
            parts = [c.get("thinking", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "thinking" and c.get("thinking")]
            if parts:
                collected.append("\n".join(parts))
    collected.reverse()
    return "\n\n".join(collected).strip()


def split_paragraphs(text: str) -> list[str]:
    chunks, cur, in_code = [], [], False
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


def post(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{SERVER}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Auth-Token", TOKEN)
    try:
        with _OPENER.open(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {}
            return resp.status, parsed
    except Exception as e:
        log(f"POST {path} failed: {e}")
        return 0, {}


def main() -> None:
    raw = sys.stdin.read() or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    transcript_path = data.get("transcript_path") or ""
    lines = read_transcript_lines(transcript_path) if (transcript_path and os.path.exists(transcript_path)) else []

    text = (data.get("last_assistant_message") or "").strip()
    if not text and lines:
        text = assistant_text_from_lines(lines)
    if not text:
        log("empty assistant text — skip")
        return

    chunks = split_paragraphs(text) or [text]

    ok = 0
    first_turn_id = ""
    for i, chunk in enumerate(chunks):
        status, parsed = post("/chat/append", {
            "role": "assistant",
            "text": chunk,
            "source": "ccc-stop-hook",
            "ts": datetime.datetime.now(datetime.timezone.utc)
                  .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        })
        if status == 200:
            ok += 1
            if not first_turn_id:
                first_turn_id = parsed.get("turn_id") or (parsed.get("record") or {}).get("turn_id") or ""
        if i < len(chunks) - 1:
            time.sleep(GAP_SECONDS)
    log(f"posted {ok}/{len(chunks)} chunks ok")

    # 思考卡片: 挂到第一条气泡的 turn_id
    if first_turn_id and lines:
        thinking = thinking_from_lines(lines)
        if thinking:
            status, _ = post("/v1/thinking", {
                "turn_id": first_turn_id,
                "thinking": thinking,
                "session_id": "ccc-stop-hook",
            })
            if status == 200:
                log(f"posted to /v1/thinking ok (turn={first_turn_id} chars={len(thinking)})")
            else:
                log(f"POST /v1/thinking failed http={status} (non-blocking)")


if __name__ == "__main__":
    main()
