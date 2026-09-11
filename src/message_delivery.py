"""真人式回复分段与打字节奏规划。

本模块只接收已经通过对话基本要求校验的最终正文，随后生成有序消息段和
发送前延迟。它不调用模型、不访问数据库，也不依赖 OneBot，因此分句规则、
额度收敛和节奏估算都可以使用纯单元测试验证。
"""

from __future__ import annotations

import math
import random
import re
import unicodedata
from dataclasses import dataclass


# ==================== 可配置发送策略 ====================
# 用户确认的 v1 规则比较特殊：只有总长度不超过阈值的短回复才拆分；超过阈值
# 的回复维持单条发送。这里保留成显式策略字段，方便以后只通过配置调整。


@dataclass(frozen=True)
class DeliveryPolicy:
    """一次回复的拆分上限和打字时间估算参数。"""

    split_eligible_max_chars: int = 60
    max_segments: int = 6
    typing_speed: float = 1.0
    cjk_seconds: float = 0.12
    other_seconds: float = 0.06
    base_seconds: float = 0.25
    jitter_min: float = 0.85
    jitter_max: float = 1.15
    min_delay_seconds: float = 0.6
    max_delay_seconds: float = 6.0

    def __post_init__(self) -> None:
        """阻止无效策略绕过 BotConfig 的环境变量校验。"""
        if self.split_eligible_max_chars < 1:
            raise ValueError("split_eligible_max_chars 必须大于 0")
        if self.max_segments < 1:
            raise ValueError("max_segments 必须大于 0")
        numeric_values = (
            self.typing_speed,
            self.cjk_seconds,
            self.other_seconds,
            self.base_seconds,
            self.jitter_min,
            self.jitter_max,
            self.min_delay_seconds,
            self.max_delay_seconds,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("打字延迟参数必须是有限数字")
        if self.typing_speed < 0:
            raise ValueError("typing_speed 不能小于 0")
        if self.cjk_seconds < 0 or self.other_seconds < 0 or self.base_seconds < 0:
            raise ValueError("单字打字时间不能小于 0")
        if self.jitter_min <= 0 or self.jitter_max < self.jitter_min:
            raise ValueError("打字延迟随机范围无效")
        if self.min_delay_seconds < 0 or self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("打字延迟上下限无效")


@dataclass(frozen=True)
class DeliveryPlan:
    """发送层可直接执行的不可变计划。"""

    segments: tuple[str, ...]
    delays: tuple[float, ...]
    total_chars: int

    @property
    def split(self) -> bool:
        """是否真的拆成了多条消息。"""
        return len(self.segments) > 1


# ==================== 标点与受保护文本识别 ====================
# 英文句点故意不作为断点，天然避免拆坏 3.14、e.g. 和域名。逗号虽然是断点，
# 但位于两个数字之间时视为千位分隔符。URL 和常见域名整体标成受保护区域。


_DELIMITERS = frozenset("。！？!?；;，,\n\r")
_STRONG_PUNCTUATION = frozenset("！？!?")
_PROTECTED_TOKEN_PATTERN = re.compile(
    r"(?:https?://|ftp://|www\.)[A-Za-z0-9:/?#\[\]@!$&'()*+\-=._~%,]+"
    r"|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[A-Za-z0-9/?#._~%+\-=]*)?",
    re.IGNORECASE,
)


def _protected_positions(text: str) -> list[bool]:
    """为 URL 和域名建立字符位置掩码，扫描分句时不破坏它们。"""
    protected = [False] * len(text)
    for match in _PROTECTED_TOKEN_PATTERN.finditer(text):
        for index in range(match.start(), match.end()):
            protected[index] = True
    return protected


def _is_delimiter(text: str, index: int, protected: list[bool]) -> bool:
    """判断当前位置能否结束分句，并排除 URL 与数字中的逗号。"""
    character = text[index]
    if character not in _DELIMITERS or protected[index]:
        return False
    if character in {",", "，"}:
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        if previous.isdigit() and following.isdigit():
            return False
    return True


def _sentence_parts(text: str) -> list[tuple[str, str]]:
    """扫描正文并返回正文与其后连续断点标点，不制造空分句。"""
    protected = _protected_positions(text)
    parts: list[tuple[str, str]] = []
    cursor = 0
    index = 0
    while index < len(text):
        if not _is_delimiter(text, index, protected):
            index += 1
            continue
        delimiter_start = index
        while index < len(text) and _is_delimiter(text, index, protected):
            index += 1
        content = text[cursor:delimiter_start]
        if content.strip():
            parts.append((content, text[delimiter_start:index]))
        cursor = index
    tail = text[cursor:]
    if tail.strip():
        parts.append((tail, ""))
    return parts


def _render_group(group: list[tuple[str, str]]) -> str:
    """合并一组分句；内部标点保留，消息末尾只保留问号和感叹号。"""
    body = "".join(content + delimiter for content, delimiter in group[:-1])
    final_content, final_delimiter = group[-1]
    strong = "".join(
        character for character in final_delimiter if character in _STRONG_PUNCTUATION
    )
    # 第二指标要求单个纯文本块，所以换行和多余空白在发送前统一压平。
    value = re.sub(r"\s+", " ", body + final_content + strong)
    return value.strip()


# ==================== 分段与延迟规划 ====================


def _split_reply(text: str, maximum_segments: int, policy: DeliveryPolicy) -> list[str]:
    """按照已确认的 60 字短回复规则生成不超过额度的消息段。"""
    if len(text) > policy.split_eligible_max_chars or maximum_segments <= 1:
        return [text]

    parts = _sentence_parts(text)
    if len(parts) <= 1:
        return [text]

    group_count = min(len(parts), maximum_segments)
    groups = [[part] for part in parts[: group_count - 1]]
    groups.append(parts[group_count - 1 :])
    segments = [_render_group(group) for group in groups]
    return [segment for segment in segments if segment] or [text]


def _typing_delay(text: str, policy: DeliveryPolicy, rng) -> float:
    """按下一段可见字符估算打字时间，并加入有界随机抖动。"""
    if policy.typing_speed <= 0:
        return 0.0
    cjk_count = 0
    other_count = 0
    for character in text:
        if character.isspace():
            continue
        if unicodedata.east_asian_width(character) in {"W", "F"}:
            cjk_count += 1
        else:
            other_count += 1
    estimated = (
        policy.base_seconds
        + cjk_count * policy.cjk_seconds
        + other_count * policy.other_seconds
    )
    jitter = float(rng.uniform(policy.jitter_min, policy.jitter_max))
    jitter = min(policy.jitter_max, max(policy.jitter_min, jitter))
    value = estimated * policy.typing_speed * jitter
    return min(policy.max_delay_seconds, max(policy.min_delay_seconds, value))


def plan_reply(
    text: str,
    available_messages: int | None = None,
    *,
    policy: DeliveryPolicy | None = None,
    rng=None,
) -> DeliveryPlan:
    """把最终正文规划为分段和逐段延迟。

    ``available_messages`` 是意愿模块当天剩余额度。额度不足时减少分组数量，
    超出的尾部会并入最后一条，而不是被丢弃。第一条始终立即发送。
    """
    active_policy = policy or DeliveryPolicy()
    value = str(text or "").strip()
    if not value:
        return DeliveryPlan((), (), 0)

    available = (
        active_policy.max_segments
        if available_messages is None
        else max(0, int(available_messages))
    )
    if available == 0:
        return DeliveryPlan((), (), len(value))
    maximum_segments = min(active_policy.max_segments, available)
    segments = tuple(_split_reply(value, maximum_segments, active_policy))
    active_rng = rng if rng is not None else random.SystemRandom()
    delays = tuple(
        0.0 if index == 0 else _typing_delay(segment, active_policy, active_rng)
        for index, segment in enumerate(segments)
    )
    return DeliveryPlan(segments, delays, len(value))
