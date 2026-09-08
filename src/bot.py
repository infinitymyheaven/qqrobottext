#!/usr/bin/env python3
"""QQ 群聊机器人：被 @ 时调用 DeepSeek 生成回复。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
from collections import defaultdict
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import websockets

RETRY_DELAY_SECONDS = 3.0
ACTION_TIMEOUT_SECONDS = 10.0
DEFAULT_WS_URL = "ws://127.0.0.1:3001"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_SYSTEM_PROMPT = "你是一个友好、简洁的 QQ 群聊助手。请直接回答用户的问题。"
DEFAULT_ERROR_REPLY = "抱歉，AI 服务暂时不可用，请稍后再试。"
DEFAULT_EMPTY_REPLY = "请在 @ 我后输入想聊的内容。"
ENV_FILE = Path(".env")

_CQ_AT_PATTERN = re.compile(r"\[CQ:at(?:,([^\]]*))?\]")
_CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]+\]")

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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def with_access_token(url: str, token: str) -> str:
    """把 Access Token 以 access_token 查询参数附加到 WS 地址。"""
    if not token:
        return url
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "access_token"
    ]
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


def extract_message_text(payload: dict) -> str:
    """提取用户输入的纯文本，忽略 @、图片等非文本消息段。"""
    message = payload.get("message")
    if isinstance(message, list):
        parts = []
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            text = (segment.get("data") or {}).get("text")
            if text is not None:
                parts.append(str(text))
        return "".join(parts).strip()

    raw = str(payload.get("raw_message") or message or "")
    return unescape(_CQ_CODE_PATTERN.sub("", raw)).strip()


class DeepSeekAPIError(RuntimeError):
    """DeepSeek 请求或响应异常。"""


class DeepSeekClient:
    """使用 DeepSeek 的 OpenAI 兼容 HTTP 接口生成聊天回复。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        timeout_seconds: float = 60.0,
        max_tokens: int = 1024,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    async def chat(self, history: list[dict], user_text: str) -> str:
        """异步生成回复；阻塞式标准库 HTTP 调用在线程中执行。"""
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
        return await asyncio.to_thread(self._request, messages)

    def _request(self, messages: list[dict]) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": self.max_tokens,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise DeepSeekAPIError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DeepSeekAPIError(f"DeepSeek 请求失败：{exc}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekAPIError("DeepSeek 返回了无法识别的数据") from exc
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekAPIError("DeepSeek 返回了空回复")
        return content.strip()


class QQBot:
    """连接 NapCat（OneBot v11 正向 WS）并处理群聊。"""

    def __init__(
        self,
        ws_url: str,
        token: str = "",
        *,
        llm_client: DeepSeekClient | None = None,
        max_history_messages: int = 10,
        max_reply_chars: int = 2000,
    ) -> None:
        self.log_url = ws_url
        self.ws_url = with_access_token(ws_url, token)
        self.llm_client = llm_client
        self.max_history_messages = max(0, max_history_messages)
        self.max_reply_chars = max(1, max_reply_chars)
        self._histories: dict[tuple[str, str], list[dict]] = {}
        self._conversation_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def run(self) -> None:
        """持续运行：连接失败或断开后每 3 秒自动重连。"""
        logger.info("正在连接 NapCat（%s）...", self.log_url)
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    logger.info("已连接 NapCat OneBot WebSocket。")
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
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
            if not self_id or user_id == self_id or group_id is None:
                return
            if not is_at_self(payload, self_id):
                return

            question = extract_message_text(payload)
            if not question:
                await self._reply(ws, group_id, DEFAULT_EMPTY_REPLY, pending)
                return
            if self.llm_client is None:
                logger.error("未配置 DeepSeek 客户端")
                await self._reply(ws, group_id, DEFAULT_ERROR_REPLY, pending)
                return

            conversation_id = (str(group_id), user_id)
            async with self._conversation_locks[conversation_id]:
                history = self._histories.get(conversation_id, [])
                logger.info("群 %s 的用户 %s 正在请求 AI 回复", group_id, user_id)
                try:
                    answer = await self.llm_client.chat(history, question)
                except Exception:  # noqa: BLE001
                    logger.exception("调用 DeepSeek 失败")
                    await self._reply(ws, group_id, DEFAULT_ERROR_REPLY, pending)
                    return

                updated = history + [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": answer},
                ]
                if self.max_history_messages:
                    updated = updated[-self.max_history_messages :]
                else:
                    updated = []
                self._histories[conversation_id] = updated
                await self._reply(ws, group_id, answer, pending)
        except Exception:  # noqa: BLE001
            logger.exception("处理群消息失败")

    async def _reply(self, ws, group_id, text: str, pending: dict) -> None:
        reply = text
        if len(reply) > self.max_reply_chars:
            reply = reply[: self.max_reply_chars].rstrip() + "…"
        await self._send_action(
            ws,
            "send_group_msg",
            {"group_id": group_id, "message": reply},
            pending,
        )

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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("环境变量 %s 不是整数，使用默认值 %s", name, default)
        return default


def main() -> int:
    load_env_file()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        logger.error("缺少 DEEPSEEK_API_KEY，请在 .env 中填写 DeepSeek API Key。")
        return 2

    llm_client = DeepSeekClient(
        api_key,
        base_url=(os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).strip(),
        model=(os.getenv("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip(),
        system_prompt=os.getenv("DEEPSEEK_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT).strip(),
        timeout_seconds=_env_int("DEEPSEEK_TIMEOUT_SECONDS", 60),
        max_tokens=_env_int("DEEPSEEK_MAX_TOKENS", 1024),
    )
    bot = QQBot(
        (os.getenv("NAPCAT_WS_URL") or DEFAULT_WS_URL).strip(),
        (os.getenv("NAPCAT_WS_TOKEN") or "").strip(),
        llm_client=llm_client,
        max_history_messages=_env_int("CHAT_HISTORY_MESSAGES", 10),
        max_reply_chars=_env_int("MAX_REPLY_CHARS", 2000),
    )

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("已手动退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
