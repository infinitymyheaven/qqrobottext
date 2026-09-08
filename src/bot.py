#!/usr/bin/env python3
"""极简 QQ 群聊机器人（Python 版）。

行为：群聊中收到“@机器人本人”的消息时，回复纯文本“对不起做不到。”。
其余消息（普通群消息、@全体成员、私聊、机器人自己的消息）一律不响应。

数据通道：连接 NapCat 的 OneBot v11 正向 WebSocket 服务
（默认 ws://127.0.0.1:3001）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import websockets

REPLY_TEXT = "对不起做不到。"
RETRY_DELAY_SECONDS = 3.0
ACTION_TIMEOUT_SECONDS = 10.0
DEFAULT_WS_URL = "ws://127.0.0.1:3001"
ENV_FILE = Path(".env")

_CQ_AT_PATTERN = re.compile(r"\[CQ:at(?:,([^\]]*))?\]")

logger = logging.getLogger("qqrobot")


def load_env_file(path: Path = ENV_FILE) -> None:
    """极简 .env 读取：仅设置尚未存在于 os.environ 中的 KEY=VALUE。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def with_access_token(url: str, token: str) -> str:
    """把 Access Token 以 access_token 查询参数附加到 WS 地址。"""
    if not token:
        return url
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "access_token"]
    query.append(("access_token", token))
    return urlunparse(parsed._replace(query=urlencode(query)))


def is_at_self(payload: dict, self_id: str) -> bool:
    """判断消息是否 @ 了机器人本人；@全体成员(qq=all) 不算。"""
    message = payload.get("message")
    if isinstance(message, list):
        return any(
            isinstance(segment, dict)
            and segment.get("type") == "at"
            and str((segment.get("data") or {}).get("qq")) == self_id
            for segment in message
        )

    # 兜底：NapCat 若配置为字符串消息格式，则解析 raw_message 中的 CQ 码。
    raw = str(payload.get("raw_message") or "")
    for match in _CQ_AT_PATTERN.finditer(raw):
        params = match.group(1) or ""
        for part in params.split(","):
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            if key.strip() == "qq" and value.strip() == self_id:
                return True
    return False


class QQBot:
    """连接 NapCat（OneBot v11 正向 WS）并处理群消息的客户端。"""

    def __init__(self, ws_url: str, token: str = "") -> None:
        self.log_url = ws_url
        self.ws_url = with_access_token(ws_url, token)

    async def run(self) -> None:
        """持续运行：连接失败或断开后每 3 秒自动重连，不崩溃。"""
        logger.info("正在连接 NapCat（%s）...", self.log_url)
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    logger.info("已连接 NapCat OneBot WebSocket。")
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 需要兜住所有连接异常后重连
                logger.warning("连接异常：%s", exc)
            logger.info("与 NapCat 的连接已断开，稍后自动重连...")
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            logger.info("正在连接 NapCat（%s）...", self.log_url)

    async def _read_loop(self, ws, pending: dict | None = None) -> None:
        """读取消息：动作响应按 echo 回填；群消息事件交给独立任务处理。"""
        pending = {} if pending is None else pending
        event_tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue

                echo = payload.get("echo")
                if echo is not None and echo in pending:
                    future = pending.pop(echo)
                    if not future.done():
                        if payload.get("status") == "ok" and payload.get("retcode") == 0:
                            future.set_result(payload)
                        else:
                            future.set_exception(
                                RuntimeError(
                                    f"动作失败：{json.dumps(payload, ensure_ascii=False)}"
                                )
                            )
                    continue

                if (
                    payload.get("post_type") == "message"
                    and payload.get("message_type") == "group"
                ):
                    task = asyncio.create_task(
                        self._handle_group_message(ws, payload, pending)
                    )
                    event_tasks.add(task)
                    task.add_done_callback(event_tasks.discard)
        finally:
            error = RuntimeError("WebSocket 连接已断开")
            for future in pending.values():
                if not future.done():
                    future.set_exception(error)
            if event_tasks:
                await asyncio.gather(*event_tasks, return_exceptions=True)

    async def _handle_group_message(self, ws, payload: dict, pending: dict) -> None:
        try:
            self_id = str(payload.get("self_id") or "")
            user_id = str(payload.get("user_id") or "")
            group_id = payload.get("group_id")

            # 忽略机器人自己发出的消息，避免自触发。
            if not self_id or user_id == self_id:
                return
            if group_id is None:
                return
            if not is_at_self(payload, self_id):
                return

            logger.info(
                "群 %s 收到 @机器人 消息（发送者 %s），回复：%s",
                group_id,
                user_id,
                REPLY_TEXT,
            )
            await self._send_action(
                ws,
                "send_group_msg",
                {"group_id": group_id, "message": REPLY_TEXT},
                pending,
            )
        except Exception:  # noqa: BLE001
            logger.exception("处理群消息失败")

    async def _send_action(self, ws, action: str, params: dict, pending: dict) -> None:
        echo = f"{random.randrange(1 << 48):x}-{id(ws)}"
        future = asyncio.get_running_loop().create_future()
        pending[echo] = future
        try:
            await ws.send(
                json.dumps(
                    {"action": action, "params": params, "echo": echo},
                    ensure_ascii=False,
                )
            )
            await asyncio.wait_for(future, timeout=ACTION_TIMEOUT_SECONDS)
        finally:
            pending.pop(echo, None)


def main() -> int:
    load_env_file()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ws_url = (os.getenv("NAPCAT_WS_URL") or DEFAULT_WS_URL).strip()
    token = (os.getenv("NAPCAT_WS_TOKEN") or "").strip()
    bot = QQBot(ws_url, token)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("已手动退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
