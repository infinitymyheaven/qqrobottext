#!/usr/bin/env python3
"""独立人格采集、版本管理和离线盲测命令。

运行本工具前，用户应在第二个 NapCat 实例中亲自完成二维码登录。本工具只连接
本机 OneBot WebSocket，不处理 QQ 密码，也不会把聊天原文写入 SQLite 或日志。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import websockets

if __package__:
    from .bot import (
        DEFAULT_DEEPSEEK_BASE_URL,
        DEFAULT_DEEPSEEK_MODEL,
        DeepSeekClient,
        OneBotActionError,
        _onebot_failure_message,
        extract_message_text,
        load_env_file,
        with_access_token,
    )
    from .memory import MemoryStore
    from .persona import (
        PersonaContentEngine,
        PersonaSample,
        merge_persona_profiles,
        sanitize_samples,
    )
    from .persona_notification import notify_persona_draft
else:  # 支持 README 中的 `python src\persona_collector.py` 运行方式。
    from bot import (
        DEFAULT_DEEPSEEK_BASE_URL,
        DEFAULT_DEEPSEEK_MODEL,
        DeepSeekClient,
        OneBotActionError,
        _onebot_failure_message,
        extract_message_text,
        load_env_file,
        with_access_token,
    )
    from memory import MemoryStore
    from persona import (
        PersonaContentEngine,
        PersonaSample,
        merge_persona_profiles,
        sanitize_samples,
    )
    from persona_notification import notify_persona_draft


DEFAULT_HISTORY_DAYS = 90
DEFAULT_TARGET_MESSAGE_LIMIT = 20_000
DEFAULT_PAGE_SIZE = 100
DEFAULT_CONTEXT_BEFORE = 3
DEFAULT_CONTEXT_AFTER = 1


@dataclass(frozen=True)
class Conversation:
    """用户在本机终端选择的一段可访问会话。"""

    key: str
    kind: str  # group / private
    peer_id: str
    label: str


class OneBotRPC:
    """采集器使用的最小 OneBot RPC；不会发送任何聊天消息。"""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.sequence = 0

    async def request(self, action: str, params: dict | None = None) -> object:
        self.sequence += 1
        echo = f"persona-{self.sequence}"
        await self.ws.send(
            json.dumps(
                {"action": action, "params": params or {}, "echo": echo},
                ensure_ascii=False,
            )
        )
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=20)
            payload = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
            if not isinstance(payload, dict) or payload.get("echo") != echo:
                continue
            if payload.get("status") != "ok" or payload.get("retcode") != 0:
                raise OneBotActionError(
                    payload.get("retcode"), _onebot_failure_message(payload)
                )
            return payload.get("data")


# ==================== 会话发现和终端选择 ====================


async def list_conversations(rpc: OneBotRPC) -> list[Conversation]:
    """读取群和好友列表；真实 ID 只在本机选择界面中短暂显示。"""
    groups = await rpc.request("get_group_list", {"no_cache": True}) or []
    friends = await rpc.request("get_friend_list", {}) or []
    output: list[Conversation] = []
    for item in groups if isinstance(groups, list) else []:
        peer_id = str(item.get("group_id") or "")
        if peer_id:
            output.append(
                Conversation(
                    key=f"group:{peer_id}",
                    kind="group",
                    peer_id=peer_id,
                    label=str(item.get("group_name") or "未命名群"),
                )
            )
    for item in friends if isinstance(friends, list) else []:
        peer_id = str(item.get("user_id") or "")
        if peer_id:
            output.append(
                Conversation(
                    key=f"private:{peer_id}",
                    kind="private",
                    peer_id=peer_id,
                    label=str(item.get("remark") or item.get("nickname") or "未命名好友"),
                )
            )
    return output


def parse_selection(value: str, count: int) -> list[int]:
    """解析 ``1,3-5`` 或 ``all``，保持用户输入顺序并去重。"""
    text = value.strip().casefold()
    if text == "all":
        return list(range(count))
    selected: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            numbers = range(int(start_text), int(end_text) + 1)
        else:
            numbers = (int(part),)
        for number in numbers:
            index = number - 1
            if index < 0 or index >= count:
                raise ValueError(f"选择序号超出范围：{number}")
            if index not in selected:
                selected.append(index)
    if not selected:
        raise ValueError("至少选择一个会话")
    return selected


def choose_conversations(items: list[Conversation]) -> list[Conversation]:
    """在终端列出会话并要求显式选择，绝不默认扫描全部会话。"""
    if not items:
        raise RuntimeError("当前账号没有可采集的群聊或好友会话")
    print("可选择的会话（这些名称只显示在本机终端，不写入数据库）：")
    for index, item in enumerate(items, 1):
        kind = "群" if item.kind == "group" else "私聊"
        print(f"  {index:>3}. [{kind}] {item.label} ({item.peer_id})")
    while True:
        raw = input("请输入序号（例如 1,3-5；输入 all 代表全部）：")
        try:
            return [items[index] for index in parse_selection(raw, len(items))]
        except (ValueError, TypeError) as exc:
            print(f"选择无效：{exc}")


# ==================== 历史标准化和样本构造 ====================


def _history_messages(data: object) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("messages") or data.get("message_list") or []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _message_user_id(message: dict) -> str:
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    # 私聊历史的顶层 user_id 在部分实现中代表会话对端；sender 才是实际发言者。
    return str(sender.get("user_id") or message.get("user_id") or "")


def _message_time(message: dict) -> float:
    try:
        return float(message.get("time") or message.get("message_time") or 0)
    except (TypeError, ValueError):
        return 0.0


def _message_id(message: dict) -> str:
    return str(message.get("message_id") or message.get("message_seq") or message.get("msg_id") or "")


def build_persona_samples(
    messages: list[dict],
    target_user_id: str,
    scene: str,
    *,
    before: int = DEFAULT_CONTEXT_BEFORE,
    after: int = DEFAULT_CONTEXT_AFTER,
    eligible_message_hashes: set[str] | None = None,
) -> list[PersonaSample]:
    """从同一会话按时间生成“有限上下文 → 本人回复”样本。"""
    ordered = sorted(messages, key=_message_time)
    aliases: dict[str, str] = {}
    anonymous_ids: dict[str, str] = {}

    def speaker_label(message: dict) -> str:
        uid = _message_user_id(message)
        if uid == str(target_user_id):
            label = "模板用户"
        else:
            label = anonymous_ids.setdefault(uid or "unknown", f"成员{len(anonymous_ids) + 1}")
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        for raw_name in (sender.get("card"), sender.get("nickname")):
            if raw_name:
                aliases[str(raw_name)] = label
        return label

    labels = [speaker_label(item) for item in ordered]
    samples: list[PersonaSample] = []
    for index, message in enumerate(ordered):
        if _message_user_id(message) != str(target_user_id):
            continue
        if (
            eligible_message_hashes is not None
            and _message_identity_hash(message) not in eligible_message_hashes
        ):
            continue
        response = extract_message_text(message)
        if not response:
            continue
        context_parts: list[str] = []
        start = max(0, index - before)
        # 生成条件严格只含回复前消息；回复后的消息属于结果反馈，不能造成未来泄漏。
        for context_index in range(start, index):
            text = extract_message_text(ordered[context_index])
            if text:
                context_parts.append(f"{labels[context_index]}：{text}")
        outcome = ""
        for outcome_index in range(index + 1, min(len(ordered), index + after + 1)):
            outcome = extract_message_text(ordered[outcome_index])
            if outcome:
                break
        samples.append(
            PersonaSample(
                context="\n".join(context_parts),
                response=response,
                scene="private" if scene == "private" else "group",
                sent_at=_message_time(message),
                outcome=outcome,
            )
        )
    return sanitize_samples(samples, aliases)


def _page_fingerprint(messages: list[dict]) -> str:
    values = "|".join(_message_id(item) or str(_message_time(item)) for item in messages)
    return hashlib.sha256(values.encode("utf-8")).hexdigest()


def _message_identity_hash(message: dict) -> str:
    """持久化消息身份的哈希而非 OneBot 原始 ID，用于跨进程去重。"""
    identity = _message_id(message) or f"{_message_time(message)}:{_message_user_id(message)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _conversation_storage_key(conversation: Conversation) -> str:
    """把真实会话 ID 转为稳定不可逆键；数据库不保存群号或好友 QQ。"""
    digest = hashlib.sha256(
        f"{conversation.kind}:{conversation.peer_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{conversation.kind}:{digest}"


async def collect_conversation(
    rpc: OneBotRPC,
    llm: DeepSeekClient,
    store: MemoryStore,
    conversation: Conversation,
    target_user_id: str,
    *,
    cutoff: float,
    remaining: int,
    seed_background: str,
    public_metadata: dict,
    restart: bool,
) -> tuple[dict | None, dict]:
    """逐页采集并在每页后保存派生断点；原始页数据随后即可释放。"""
    storage_key = _conversation_storage_key(conversation)
    previous_state = {} if restart else store.get_persona_collection_state(
        target_user_id, storage_key
    )
    if previous_state.get("completed"):
        return previous_state.get("derived_profile") or None, {
            "conversation": storage_key,
            "display": conversation.label,
            "resumed": True,
            "scanned": int(previous_state.get("scanned_count") or 0),
            "target_messages": int(
                (previous_state.get("derived_profile") or {}).get("source_message_count") or 0
            ),
            "gap": "使用已完成的派生断点",
        }

    profiles: list[dict] = []
    if previous_state.get("derived_profile"):
        profiles.append(previous_state["derived_profile"])
    cursor = str(previous_state.get("cursor") or "")
    previous_fingerprint = str(previous_state.get("fingerprint") or "")
    scanned = int(previous_state.get("scanned_count") or 0)
    oldest_at = float(previous_state.get("oldest_at") or 0)
    newest_at = float(previous_state.get("newest_at") or 0)
    target_count = sum(int(item.get("source_message_count") or 0) for item in profiles)
    processed_hashes: set[str] = set(previous_state.get("processed_hashes") or [])
    gap = ""
    deferred_page: list[dict] = []
    fetch_cursor = cursor

    async def process_page(page: list[dict], older_page: list[dict] | None = None) -> bool:
        """提炼一张已确认的页；更早页只以内存边界参与语境。"""
        nonlocal cursor, scanned, oldest_at, newest_at, target_count
        page_hashes = {_message_identity_hash(item) for item in page}
        eligible_hashes = {
            _message_identity_hash(item)
            for item in page
            if not _message_time(item) or _message_time(item) >= cutoff
        }
        older_boundary = sorted(older_page or [], key=_message_time)[-DEFAULT_CONTEXT_BEFORE:]
        samples = build_persona_samples(
            [*older_boundary, *page],
            target_user_id,
            conversation.kind,
            eligible_message_hashes=eligible_hashes,
        )
        samples = samples[: max(0, remaining - target_count)]
        if samples:
            profile = await llm.analyze_persona_samples(
                samples,
                public_metadata=public_metadata,
                seed_background=seed_background,
                now=time.time(),
            )
            profiles.append(profile)
            target_count += len(samples)

        processed_hashes.update(page_hashes)
        scanned += len(page)
        page_times = [value for value in map(_message_time, page) if value]
        if page_times:
            oldest_at = min([oldest_at, *page_times]) if oldest_at else min(page_times)
            newest_at = max([newest_at, *page_times])
        oldest_message = min(page, key=_message_time)
        cursor = str(oldest_message.get("message_seq") or _message_id(oldest_message))
        completed = bool(page_times and min(page_times) < cutoff)
        merged = merge_persona_profiles(profiles) if profiles else {}
        store.save_persona_collection_state(
            target_user_id,
            storage_key,
            {
                "conversation_type": conversation.kind,
                "cursor": cursor,
                "fingerprint": _page_fingerprint(page),
                "oldest_at": oldest_at,
                "newest_at": newest_at,
                "scanned_count": scanned,
                "processed_hashes": sorted(processed_hashes)[-50_000:],
                "derived_profile": merged,
                "completed": completed,
                "updated_at": time.time(),
            },
        )
        return completed

    for _page_number in range(200):
        if target_count >= remaining:
            gap = "达到本次目标消息上限"
            break
        if conversation.kind == "group":
            params: dict = {
                "group_id": conversation.peer_id,
                "count": DEFAULT_PAGE_SIZE,
                "disable_get_url": True,
                "parse_mult_msg": False,
            }
            if fetch_cursor:
                params["message_seq"] = fetch_cursor
            action = "get_group_msg_history"
        else:
            params = {
                "user_id": conversation.peer_id,
                "count": DEFAULT_PAGE_SIZE,
                "disable_get_url": True,
                "parse_mult_msg": False,
            }
            action = "get_friend_msg_history"
        try:
            page = _history_messages(await rpc.request(action, params))
        except Exception as exc:  # noqa: BLE001 - 单会话失败必须降级并报告覆盖缺口。
            gap = f"历史接口停止：{type(exc).__name__}"
            break
        returned_count = len(page)
        page = [item for item in page if _message_identity_hash(item) not in processed_hashes]
        if not page:
            if returned_count:
                gap = "历史接口返回重复页，已停止回溯"
            break
        fingerprint = _page_fingerprint(page)
        if fingerprint == previous_fingerprint:
            gap = "历史接口返回重复页，已停止回溯"
            break
        previous_fingerprint = fingerprint
        oldest_message = min(page, key=_message_time)
        fetch_cursor = str(oldest_message.get("message_seq") or _message_id(oldest_message))
        if deferred_page:
            if await process_page(deferred_page, page):
                deferred_page = []
                break
        deferred_page = page
        page_times = [value for value in map(_message_time, page) if value]
        if page_times and min(page_times) < cutoff:
            await process_page(deferred_page)
            deferred_page = []
            break
        # 当前 NapCat 私聊接口没有稳定的跨版本分页参数；重复请求只会泄漏成本。
        if conversation.kind == "private":
            gap = "私聊历史接口仅返回当前可用页"
            break

    if deferred_page and target_count < remaining:
        await process_page(deferred_page)

    merged = merge_persona_profiles(profiles) if profiles else None
    store.save_persona_collection_state(
        target_user_id,
        storage_key,
        {
            "conversation_type": conversation.kind,
            "cursor": cursor,
            "fingerprint": previous_fingerprint,
            "oldest_at": oldest_at,
            "newest_at": newest_at,
            "scanned_count": scanned,
            "processed_hashes": sorted(processed_hashes)[-50_000:],
            "derived_profile": merged or {},
            "completed": True,
            "updated_at": time.time(),
        },
    )
    return merged, {
        "conversation": storage_key,
        "display": conversation.label,
        "resumed": bool(previous_state),
        "scanned": scanned,
        "target_messages": target_count,
        "oldest_at": oldest_at,
        "newest_at": newest_at,
        "gap": gap,
    }


# ==================== 命令实现 ====================


def _persona_user_id() -> str:
    canonical = (os.getenv("PERSONA_USER_ID") or "").strip()
    legacy = (os.getenv("WILLINGNESS_PERSONA_USER_ID") or "").strip()
    if canonical and legacy and canonical != legacy:
        raise ValueError("PERSONA_USER_ID 与旧配置不一致")
    value = canonical or legacy
    if not value.isdigit():
        raise ValueError("请先在本机 .env 设置 PERSONA_USER_ID")
    return value


def _deepseek_client(*, web_search_enabled: bool = False) -> DeepSeekClient:
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("缺少 DEEPSEEK_API_KEY")
    return DeepSeekClient(
        api_key,
        base_url=(os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).strip(),
        model=(os.getenv("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL).strip(),
        web_search_enabled=web_search_enabled,
    )


async def collect_command(args) -> int:
    target = _persona_user_id()
    # Token 只使用隐藏输入，不提供命令行参数，避免进入 PowerShell 历史和进程列表。
    token = getpass.getpass("第二 NapCat WebSocket Token（留空表示未启用）：")
    url = with_access_token(args.ws_url, token)
    store = MemoryStore(args.database)
    try:
        async with websockets.connect(url) as ws:
            rpc = OneBotRPC(ws)
            login = await rpc.request("get_login_info", {}) or {}
            login_id = str(login.get("user_id") or "") if isinstance(login, dict) else ""
            if login_id != target:
                raise RuntimeError("当前第二 NapCat 登录账号与 PERSONA_USER_ID 不一致")
            conversations = choose_conversations(await list_conversations(rpc))
            try:
                metadata = await rpc.request("get_stranger_info", {"user_id": target, "no_cache": True}) or {}
            except Exception:  # noqa: BLE001 - 公开资料缺失不应阻断发言风格采集。
                metadata = {}
            if isinstance(metadata, dict):
                # 账号字段和名字没有建模必要；只保留允许公开访问的非标识属性。
                metadata = {
                    key: metadata[key]
                    for key in ("sex", "age", "level", "long_nick")
                    if metadata.get(key) not in (None, "")
                }
            llm = _deepseek_client()
            cutoff = time.time() - args.days * 86400
            profiles: list[dict] = []
            coverage: list[dict] = []
            remaining = args.limit
            for conversation in conversations:
                if remaining <= 0:
                    break
                profile, report = await collect_conversation(
                    rpc,
                    llm,
                    store,
                    conversation,
                    target,
                    cutoff=cutoff,
                    remaining=remaining,
                    seed_background=(
                        os.getenv("WILLINGNESS_PERSONAL_BACKGROUND")
                        or "喜欢自然、友好且有分寸的群聊。"
                    ),
                    public_metadata=metadata,
                    restart=args.restart,
                )
                coverage.append(report)
                if profile:
                    profiles.append(profile)
                    remaining -= int(profile.get("source_message_count") or 0)
            if not profiles:
                raise RuntimeError("所选会话中没有取得可用于建模的本人文字消息")
            final_profile = merge_persona_profiles(
                profiles, group_weight=args.group_weight
            )
            try:
                final_profile["summary"] = await llm.synthesize_persona_summary(
                    profiles, final_profile
                )
            except Exception:  # noqa: BLE001 - 保留确定性摘要即可继续生成草稿。
                print("人格摘要综合失败，已保留本地确定性摘要。", file=sys.stderr)
            # 真实会话名称只用于当前终端输出，不进入最终画像覆盖 JSON。
            stored_coverage = [
                {key: value for key, value in report.items() if key != "display"}
                for report in coverage
            ]
            final_profile["coverage"] = {
                "history_days": args.days,
                "target_limit": args.limit,
                "selected_conversations": len(conversations),
                "reports": stored_coverage,
                "best_effort": True,
            }
            version = store.save_persona_version(
                target, final_profile, time.time(), activate=False
            )
            print(f"人格草稿版本 {version} 已生成，尚未影响线上机器人。")
            print(
                f"实际提炼本人消息 {final_profile['source_message_count']} 条；"
                f"群聊 {final_profile['group_message_count']}，私聊 {final_profile['private_message_count']}。"
            )
            for item in coverage:
                print(
                    f"- {item['display']}：扫描 {item['scanned']} 条，"
                    f"提炼 {item['target_messages']} 条，缺口：{item.get('gap') or '未发现'}"
                )
            print(f"请运行 evaluate {version} 完成真实留出评测，再使用 activate 激活该版本。")
            notify_persona_draft(
                version,
                int(final_profile.get("source_message_count") or 0),
                enabled=(os.getenv("PERSONA_DRAFT_NOTIFICATIONS_ENABLED", "true").strip().casefold()
                         not in {"0", "false", "no", "off"}),
            )
            return 0
    finally:
        store.close()


def list_command(args) -> int:
    store = MemoryStore(args.database)
    try:
        versions = store.list_persona_versions(_persona_user_id())
        if not versions:
            print("尚无人格版本。")
            return 0
        for item in versions:
            created = datetime.fromtimestamp(item["created_at"]).isoformat(timespec="seconds")
            print(
                f"v{item['version']} {item['status']}，本人消息 {item['source_message_count']}，"
                f"群聊/私聊 {item['group_message_count']}/{item['private_message_count']}，创建于 {created}"
            )
        return 0
    finally:
        store.close()


def activate_command(args) -> int:
    store = MemoryStore(args.database)
    try:
        store.activate_persona_version(
            _persona_user_id(),
            args.version,
            require_passed_evaluation=True,
            force=bool(args.force),
            reason=str(args.reason or ""),
            activated_at=time.time(),
        )
        print(f"人格版本 {args.version} 已激活；重启机器人后生效。")
        return 0
    finally:
        store.close()


def rollback_command(args) -> int:
    store = MemoryStore(args.database)
    try:
        version = store.rollback_persona_version(_persona_user_id())
        print(f"已回退并激活人格版本 {version}；重启机器人后生效。")
        return 0
    finally:
        store.close()


def delete_command(args) -> int:
    target = _persona_user_id()
    if not args.yes and input("输入 DELETE 确认删除该账号的全部人格派生数据：").strip() != "DELETE":
        print("已取消。")
        return 1
    store = MemoryStore(args.database)
    try:
        store.delete_persona_data(target)
        print("人格画像、脱敏样例和采集断点已删除；原始聊天本来就未落库。")
        return 0
    finally:
        store.close()


BLIND_TEST_SCENARIOS = (
    "群里突然冷场了，你会说什么？",
    "有人说今天特别累，你怎么接话？",
    "大家在讨论要不要熬夜打游戏。",
    "朋友分享了一个离谱但好笑的新闻。",
    "有人问你周末准备做什么。",
    "群友对你的观点表示不同意。",
    "有人夸你刚才说得有道理。",
    "大家聊到最近学的新东西。",
    "有人发了一个只有他自己懂的梗。",
    "群里有人因为小事争起来了。",
    "朋友问你觉得人工智能怎么样。",
    "有人推荐了一款新游戏。",
    "群友说自己考试发挥不好。",
    "大家讨论今晚吃什么。",
    "有人半夜还在群里刷屏。",
    "群友问一个你不确定的问题。",
    "有人突然@你但没说具体事情。",
    "大家在回忆以前发生的趣事。",
    "朋友分享了一张很有意思的图片。",
    "群聊准备结束，大家陆续去休息。",
)


async def compare_command(args) -> int:
    """生成不标注来源的 A/B 回复；答案键单独保存，避免主观先入为主。"""
    target = _persona_user_id()
    store = MemoryStore(args.database)
    try:
        engine = PersonaContentEngine(
            store,
            target,
            os.getenv("WILLINGNESS_PERSONAL_BACKGROUND") or "自然、友好、有分寸",
        )
        baseline = engine.build_factor()
        candidate = engine.build_factor(version=args.version)
        if candidate is None:
            raise ValueError("指定版本无法生成人格内容因子")
        llm = _deepseek_client(web_search_enabled=False)
        rng = random.SystemRandom()
        report_lines = ["人格离线盲测：请逐题选择更像模板账号的 A 或 B。", ""]
        answer_key: list[dict] = []
        for index, scenario in enumerate(BLIND_TEST_SCENARIOS, 1):
            old_answer = await llm.chat(
                [], scenario, content_factors=(baseline,) if baseline else ()
            )
            new_answer = await llm.chat([], scenario, content_factors=(candidate,))
            candidate_is_a = bool(rng.randrange(2))
            answer_a, answer_b = (
                (new_answer, old_answer) if candidate_is_a else (old_answer, new_answer)
            )
            report_lines.extend(
                [f"{index}. {scenario}", f"A: {answer_a}", f"B: {answer_b}", ""]
            )
            answer_key.append(
                {"question": index, "candidate": "A" if candidate_is_a else "B"}
            )
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = output_dir / f"persona_blind_{stamp}.txt"
        key_path = output_dir / f"persona_blind_{stamp}_key.json"
        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        key_path.write_text(json.dumps(answer_key, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"盲测题：{report_path}")
        print(f"答案键：{key_path}（完成选择前不要打开）")
        return 0
    finally:
        store.close()


async def _evaluation_samples(
    rpc: OneBotRPC,
    conversations: list[Conversation],
    target_user_id: str,
    *,
    source_ended_at: float,
    limit: int,
) -> list[PersonaSample]:
    """临时读取严格晚于画像截止时间的样本；正文不会离开当前进程。"""
    collected: list[PersonaSample] = []
    for conversation_index, conversation in enumerate(conversations, 1):
        rows: list[dict] = []
        cursor = ""
        seen: set[str] = set()
        for _page in range(200):
            params = {
                "group_id" if conversation.kind == "group" else "user_id": conversation.peer_id,
                "count": DEFAULT_PAGE_SIZE,
                "disable_get_url": True,
                "parse_mult_msg": False,
            }
            if cursor and conversation.kind == "group":
                params["message_seq"] = cursor
            action = (
                "get_group_msg_history"
                if conversation.kind == "group"
                else "get_friend_msg_history"
            )
            try:
                page = _history_messages(await rpc.request(action, params))
            except OneBotActionError:
                # NapCat 在本地没有缓存该会话历史时会把空列表包装成
                # “消息 undefined 不存在”。单个空会话不应终止其他留出样本。
                print(
                    f"所选第 {conversation_index} 个会话没有可用的本地历史，已跳过。",
                    file=sys.stderr,
                )
                break
            new_page = [item for item in page if _message_identity_hash(item) not in seen]
            if not new_page:
                break
            for item in new_page:
                seen.add(_message_identity_hash(item))
            rows.extend(new_page)
            times = [value for value in map(_message_time, new_page) if value]
            oldest = min(new_page, key=_message_time)
            cursor = str(oldest.get("message_seq") or _message_id(oldest))
            if conversation.kind == "private" or (times and min(times) <= source_ended_at):
                break
        samples = build_persona_samples(rows, target_user_id, conversation.kind)
        collected.extend(item for item in samples if item.sent_at > source_ended_at)
    return sorted(collected, key=lambda item: item.sent_at)[:limit]


def _style_distance(reference: str, candidate: str) -> float:
    """只返回可聚合的长度与标点距离，不持久化任何正文。"""
    length_distance = abs(len(reference) - len(candidate)) / max(1, len(reference))
    punctuation = "，。！？,.!?"
    punctuation_distance = sum(
        abs(reference.count(mark) - candidate.count(mark)) for mark in punctuation
    ) / max(1, len(reference))
    return min(2.0, length_distance + punctuation_distance)


def _evaluation_passes(
    *,
    sample_count: int,
    candidate_wins: int,
    baseline_wins: int,
    safety_failures: int,
    min_samples: int,
) -> tuple[bool, float]:
    """应用固定人工门槛；平局和都不像不进入胜率分母。"""
    decisive = max(0, int(candidate_wins)) + max(0, int(baseline_wins))
    preference = max(0, int(candidate_wins)) / decisive if decisive else 0.0
    passed = (
        int(sample_count) >= int(min_samples)
        and decisive >= 10
        and preference >= 0.55
        and int(safety_failures) == 0
    )
    return passed, preference


async def evaluate_command(args) -> int:
    target = _persona_user_id()
    token = getpass.getpass("第二 NapCat WebSocket Token（留空表示未启用）：")
    store = MemoryStore(args.database)
    try:
        candidate_profile = store.get_persona_profile(target, "", version=args.version)
        if int(candidate_profile.get("version") or 0) != int(args.version):
            raise ValueError(f"人格版本不存在：{args.version}")
        source_ended_at = float(candidate_profile.get("source_ended_at") or 0)
        if source_ended_at <= 0:
            raise ValueError("候选画像缺少有效 source_ended_at，不能构造真实留出集")
        baseline_profile = store.get_persona_profile(target, "")
        baseline_version = int(baseline_profile.get("active_version") or 0)
        async with websockets.connect(with_access_token(args.ws_url, token)) as ws:
            rpc = OneBotRPC(ws)
            login = await rpc.request("get_login_info", {}) or {}
            if str((login or {}).get("user_id") or "") != target:
                raise RuntimeError("当前第二 NapCat 登录账号与 PERSONA_USER_ID 不一致")
            conversations = choose_conversations(await list_conversations(rpc))
            samples = await _evaluation_samples(
                rpc,
                conversations,
                target,
                source_ended_at=source_ended_at,
                limit=args.samples,
            )
        if len(samples) < args.min_samples:
            raise RuntimeError(
                f"画像截止时间之后只有 {len(samples)} 条可用样本，至少需要 {args.min_samples} 条"
            )

        llm = _deepseek_client(web_search_enabled=False)
        engine = PersonaContentEngine(
            store,
            target,
            os.getenv("WILLINGNESS_PERSONAL_BACKGROUND") or "自然、友好、有分寸",
        )
        rng = random.SystemRandom()
        counts = {"candidate": 0, "baseline": 0, "tie": 0, "neither": 0}
        candidate_distances: list[float] = []
        baseline_distances: list[float] = []
        safety_failures = 0
        completed = 0
        for index, sample in enumerate(samples, 1):
            prompt = sample.context or "群聊里暂时没有更早的文字上下文，请自然接话。"
            try:
                baseline_factor = engine.build_factor(
                    query=prompt, scene=sample.scene
                )
                candidate_factor = engine.build_factor(
                    query=prompt, scene=sample.scene, version=args.version
                )
                baseline_answer = await llm.chat(
                    [],
                    prompt,
                    content_factors=(baseline_factor,) if baseline_factor else (),
                    disable_thinking=True,
                )
                candidate_answer = await llm.chat(
                    [],
                    prompt,
                    content_factors=(candidate_factor,) if candidate_factor else (),
                    disable_thinking=True,
                )
            except Exception as exc:  # noqa: BLE001 - 单题失败计入安全门禁。
                safety_failures += 1
                print(f"第 {index} 题生成失败：{type(exc).__name__}", file=sys.stderr)
                continue
            candidate_is_a = bool(rng.randrange(2))
            answer_a, answer_b = (
                (candidate_answer, baseline_answer)
                if candidate_is_a
                else (baseline_answer, candidate_answer)
            )
            print(f"\n[{index}/{len(samples)}] 回复前语境：\n{sample.context or '无'}")
            print(f"真实回复：{sample.response}\nA：{answer_a}\nB：{answer_b}")
            while True:
                choice = input("选择更像模板用户的回答 [A/B/T相同/N都不像]：").strip().casefold()
                if choice in {"a", "b", "t", "n"}:
                    break
            if choice in {"a", "b"}:
                chose_candidate = (choice == "a") == candidate_is_a
                counts["candidate" if chose_candidate else "baseline"] += 1
            elif choice == "t":
                counts["tie"] += 1
            else:
                counts["neither"] += 1
            candidate_distances.append(_style_distance(sample.response, candidate_answer))
            baseline_distances.append(_style_distance(sample.response, baseline_answer))
            completed += 1

        passed, preference = _evaluation_passes(
            sample_count=completed,
            candidate_wins=counts["candidate"],
            baseline_wins=counts["baseline"],
            safety_failures=safety_failures,
            min_samples=args.min_samples,
        )
        result = {
            "version": int(args.version),
            "baseline_version": baseline_version,
            "evaluated_at": time.time(),
            "sample_count": completed,
            "candidate_wins": counts["candidate"],
            "baseline_wins": counts["baseline"],
            "ties": counts["tie"],
            "neither_count": counts["neither"],
            "candidate_preference_rate": preference,
            "safety_failures": safety_failures,
            "metrics": {
                "candidate_style_distance": sum(candidate_distances) / len(candidate_distances) if candidate_distances else 0,
                "baseline_style_distance": sum(baseline_distances) / len(baseline_distances) if baseline_distances else 0,
            },
            "passed": passed,
        }
        store.save_persona_evaluation(target, result)
        print(
            f"评测完成：候选 {counts['candidate']}，基线 {counts['baseline']}，"
            f"相同 {counts['tie']}，都不像 {counts['neither']}，候选胜率 {preference:.1%}。"
        )
        print("结果：通过，可以激活。" if passed else "结果：未通过；可继续积累样本或修订画像。")
        return 0 if passed else 1
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ 机器人结构化人格采集和版本工具")
    parser.add_argument(
        "--database", default=os.getenv("MEMORY_DB_PATH") or "data/bot_memory.sqlite3"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="从第二 NapCat 选择会话并生成草稿")
    collect.add_argument("--ws-url", default="ws://127.0.0.1:3002")
    collect.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS)
    collect.add_argument("--limit", type=int, default=DEFAULT_TARGET_MESSAGE_LIMIT)
    collect.add_argument("--group-weight", type=float, default=0.70)
    collect.add_argument("--restart", action="store_true", help="忽略旧断点重新采集")
    collect.set_defaults(handler=collect_command)

    listing = subparsers.add_parser("list", help="列出人格版本")
    listing.set_defaults(handler=list_command)
    activate = subparsers.add_parser("activate", help="激活盲测通过的版本")
    activate.add_argument("version", type=int)
    activate.add_argument("--force", action="store_true", help="应急跳过评测门禁")
    activate.add_argument("--reason", default="", help="强制激活原因，使用 --force 时必填")
    activate.set_defaults(handler=activate_command)
    rollback = subparsers.add_parser("rollback", help="回退到上一个版本")
    rollback.set_defaults(handler=rollback_command)
    delete = subparsers.add_parser("delete", help="删除全部人格派生数据")
    delete.add_argument("--yes", action="store_true")
    delete.set_defaults(handler=delete_command)
    compare = subparsers.add_parser("compare", help="生成20组新旧人格离线盲测")
    compare.add_argument("version", type=int)
    compare.add_argument("--output-dir", default="data")
    compare.set_defaults(handler=compare_command)
    evaluate = subparsers.add_parser("evaluate", help="用画像截止后的临时 QQ 样本做留出评测")
    evaluate.add_argument("version", type=int)
    evaluate.add_argument("--ws-url", default="ws://127.0.0.1:3002")
    evaluate.add_argument("--samples", type=int, default=30)
    evaluate.add_argument("--min-samples", type=int, default=10)
    evaluate.set_defaults(handler=evaluate_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "days", 1) < 1 or getattr(args, "days", 1) > 365:
        parser.error("--days 必须在 1 到 365 之间")
    if getattr(args, "limit", 1) < 1 or getattr(args, "limit", 1) > 100_000:
        parser.error("--limit 必须在 1 到 100000 之间")
    if not 0 <= getattr(args, "group_weight", 0.7) <= 1:
        parser.error("--group-weight 必须在 0 到 1 之间")
    if not 10 <= getattr(args, "samples", 30) <= 100:
        parser.error("--samples 必须在 10 到 100 之间")
    if not 10 <= getattr(args, "min_samples", 10) <= getattr(args, "samples", 30):
        parser.error("--min-samples 必须在 10 到 --samples 之间")
    if getattr(args, "force", False) and not str(getattr(args, "reason", "")).strip():
        parser.error("--force 必须同时提供非空 --reason")
    try:
        result = args.handler(args)
        return asyncio.run(result) if asyncio.iscoroutine(result) else int(result)
    except (ValueError, RuntimeError, OneBotActionError, OSError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
