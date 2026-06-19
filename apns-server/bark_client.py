"""
Bark push 客户端 — 零 Apple Developer 的 APNs 兜底方案。

Bark (https://github.com/Finb/Bark) 是开源的 iOS 推送 app。用户在 iPhone 装
Bark, 拿到一个 device key, server HTTP POST 到 api.day.app 就能推一条 banner
通知。不需要 Apple Developer 账号 / .p8 证书。

device key 来源 (优先级从高到低):
  1. config.toml [bark] device_key
  2. 环境变量 BARK_DEVICE_KEY
  3. ~/.bark_device_key 文件 (跟 docs/AI_GUIDED_SETUP_MAC.md 里的约定一致)

server base url 默认 https://api.day.app。自部署 Bark relay 的用户可在
config.toml [bark] base_url 覆盖。

隐私: Bark 推送的 title / body 会经过 Bark relay 服务器。chat 内容本体仍留在
本机, 只有通知预览过 relay。介意的话自部署一份 Bark。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BARK_BASE = "https://api.day.app"
DEFAULT_KEY_FILE = "~/.bark_device_key"


@dataclass
class BarkResponse:
    status: int
    body: str

    @property
    def ok(self) -> bool:
        return self.status == 200


def resolve_device_key(config_key: str | None) -> str:
    """按 config -> env -> ~/.bark_device_key 顺序解析 device key, 找不到返回空串。"""
    if config_key and config_key.strip():
        return config_key.strip()

    env_key = os.environ.get("BARK_DEVICE_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    key_path = Path(DEFAULT_KEY_FILE).expanduser()
    if key_path.exists():
        try:
            return key_path.read_text().strip()
        except OSError as e:
            logger.warning("bark: cannot read %s: %s", key_path, e)

    return ""


class BarkClient:
    def __init__(
        self,
        device_key: str,
        base_url: str = DEFAULT_BARK_BASE,
        timeout: float = 10.0,
    ):
        self.device_key = device_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "cc-apns-server/0.1"},
        )

    @property
    def enabled(self) -> bool:
        return bool(self.device_key)

    def close(self):
        self._client.close()

    def push(self, title: str, body: str) -> BarkResponse:
        """推一条 Bark banner 通知。device key 没配置时返回 status=0 不报错。"""
        if not self.device_key:
            return BarkResponse(status=0, body="bark disabled (no device key)")

        url = f"{self.base_url}/{self.device_key}"
        payload = {"title": title, "body": body}
        try:
            resp = self._client.post(url, json=payload)
        except httpx.HTTPError as e:
            logger.error("bark push HTTP error: %s", e)
            return BarkResponse(status=599, body=str(e))

        return BarkResponse(status=resp.status_code, body=resp.text)
