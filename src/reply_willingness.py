"""基于群消息流的拟人化回复意愿引擎。

本模块刻意不依赖 OneBot 或具体大模型协议：它接收标准化消息、读取 SQLite
知识与关系数据、计算八项特征并返回决策。网络接入和消息发送仍由 ``bot.py``
负责，因此算法可以在单元测试中使用固定时钟和固定随机数完整复现。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable

if __package__:
    from .persona import merge_persona_profiles
else:
    from persona import merge_persona_profiles


logger = logging.getLogger("qqrobot")
VECTOR_DIMENSIONS = 128


# ==================== 稳定的本地文本特征 ====================
# 话题和个人背景的实时匹配不能为每条消息调用大模型，因此使用稳定字符二元组。


def text_feature_vector(text: str) -> dict[int, float]:
    """把文本转换成跨进程稳定、稀疏且 L2 归一化的 128 维向量。"""
    normalized = "".join(re.findall(r"[\w\u4e00-\u9fff]", text.casefold()))
    if not normalized:
        return {}
    tokens = (
        [normalized]
        if len(normalized) == 1
        else [normalized[index : index + 2] for index in range(len(normalized) - 1)]
    )
    counts: dict[int, float] = defaultdict(float)
    for token in tokens:
        # Python hash() 每次启动都会变化；BLAKE2b 才能安全用于持久化维度。
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        counts[int.from_bytes(digest, "big") % VECTOR_DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in counts.values()))
    return {feature_id: value / norm for feature_id, value in counts.items()}


def cosine_similarity(left: dict[int, float], right: dict[int, float]) -> float:
    """计算两个稀疏向量的余弦相似度，并把结果约束到 0–1。"""
    if not left or not right:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    dot = sum(value * larger.get(key, 0.0) for key, value in smaller.items())
    return min(1.0, max(0.0, dot / (left_norm * right_norm)))


def average_vectors(
    weighted_vectors: Iterable[tuple[dict[int, float], float]],
) -> dict[int, float]:
    """按权重合并消息向量，生成当前消息流的话题中心。"""
    result: dict[int, float] = defaultdict(float)
    total_weight = 0.0
    for vector, weight in weighted_vectors:
        if not vector or weight <= 0:
            continue
        total_weight += weight
        for feature_id, value in vector.items():
            result[feature_id] += value * weight
    if not total_weight:
        return {}
    return {feature_id: value / total_weight for feature_id, value in result.items()}


def stable_sigmoid(value: float) -> float:
    """把任意原始分映射为概率，同时避免极端配置造成指数溢出。"""
    value = min(60.0, max(-60.0, value))
    return 1.0 / (1.0 + math.exp(-value))


# ==================== 公共数据结构 ====================


@dataclass(frozen=True)
class WillingnessConfig:
    """意愿模块的完整配置；所有时间字段统一使用秒或小时。"""

    enabled: bool = True
    message_limit: int = 500
    message_max_age_seconds: int = 10_800
    update_seconds: int = 5
    daily_reply_limit: int = 500
    user_activity: float = 0.5
    short_window_seconds: int = 600
    short_full_messages: int = 30
    old_full_messages: int = 120
    weight_user_activity: float = 0.08
    weight_group_activity: float = 0.08
    weight_topic_familiarity: float = 0.12
    weight_social_bond: float = 0.12
    weight_is_mentioned: float = 1.0
    weight_message_relevance: float = 0.12
    weight_fun_factor: float = 0.08
    weight_random_noise: float = 0.05
    sigmoid_k: float = 6.0
    sigmoid_midpoint: float = 0.65
    topic_analysis_min_hours: float = 3.0
    topic_analysis_max_hours: float = 10.0
    persona_user_id: str = ""
    personal_background: str = (
        "喜欢轻松、友好的群聊，对计算机、人工智能、游戏、网络文化和日常生活保持好奇。"
    )
    history_days: int = 30
    history_message_limit: int = 2000
    history_scan_limit: int = 10_000
    persona_increment_min_messages: int = 50
    persona_increment_max_hours: float = 24.0
    persona_increment_floor_messages: int = 10
    persona_group_style_weight: float = 0.70
    bond_inbound_rate: float = 0.12
    bond_outbound_rate: float = 0.08
    bond_grace_hours: float = 24.0
    bond_zero_days: float = 30.0
    reply_cooldown_seconds: int = 120

    def weights(self) -> dict[str, float]:
        """以可读名称返回恰好八个决策权重。"""
        return {
            "user_activity": self.weight_user_activity,
            "group_activity": self.weight_group_activity,
            "topic_familiarity": self.weight_topic_familiarity,
            "social_bond": self.weight_social_bond,
            "is_mentioned": self.weight_is_mentioned,
            "message_relevance": self.weight_message_relevance,
            "fun_factor": self.weight_fun_factor,
            "random_noise": self.weight_random_noise,
        }


@dataclass(frozen=True)
class StreamMessage:
    """内存消息流中的一条标准化消息。"""

    group_id: str
    user_id: str
    display_name: str
    sent_at: float
    text: str
    message_id: str = ""
    mentioned_bot: bool = False
    replied_to_bot: bool = False
    is_bot: bool = False


@dataclass(frozen=True)
class TopicMatch:
    """当前消息流与一个长期话题的匹配结果。"""

    topic_id: str
    name: str
    heat_level: str
    similarity: float
    familiarity: float
    emotion_intensity: float
    reply_count: int


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """每五秒刷新的群级环境状态。"""

    updated_at: float
    buffer_count: int
    recent_10m_count: int
    recent_3h_count: int
    participant_count: int
    group_activity: float
    topic_familiarity: float
    fun_factor: float
    recent_social_bond: float
    cooldown_factor: float
    topic_matches: tuple[TopicMatch, ...]


@dataclass(frozen=True)
class WillingnessFeatures:
    """一次回复意愿计算使用的八项参数。"""

    user_activity: float
    group_activity: float
    topic_familiarity: float
    social_bond: float
    is_mentioned: float
    message_relevance: float
    fun_factor: float
    random_noise: float


@dataclass(frozen=True)
class WillingnessDecision:
    """意愿计算结果以及可复算决策的全部中间值。"""

    accepted: bool
    probability: float
    raw_score: float
    random_draw: float
    features: WillingnessFeatures | None
    snapshot: EnvironmentSnapshot
    reason: str
    daily_replies: int
    daily_limit: int


# ==================== 群级消息流与回复意愿引擎 ====================


class ReplyWillingnessEngine:
    """维护群消息流、刷新环境状态并执行八参数回复决策。"""

    def __init__(self, config: WillingnessConfig, store, *, rng) -> None:
        self.config = config
        self.store = store
        self.rng = rng
        self._messages: defaultdict[str, deque[StreamMessage]] = defaultdict(deque)
        # 人格增量需要跨越三小时意愿窗口；单独队列最多保留两倍最长更新周期。
        self._persona_pending: deque[StreamMessage] = deque()
        self._snapshots: dict[str, EnvironmentSnapshot] = {}
        self._last_bot_reply_at: dict[str, float] = {}
        self._analysis_running: set[str] = set()

    def record_message(self, message: StreamMessage) -> None:
        """立即记录消息，并保持按发送时间排序及双重容量边界。"""
        queue = self._messages[message.group_id]
        if queue and message.sent_at < queue[-1].sent_at:
            ordered = sorted((*queue, message), key=lambda item: item.sent_at)
            queue.clear()
            queue.extend(ordered)
        else:
            queue.append(message)
        self._prune_group(message.group_id, message.sent_at)
        if (
            self.config.persona_user_id
            and message.user_id == self.config.persona_user_id
            and message.text
            and not message.is_bot
        ):
            if self._persona_pending and message.sent_at < self._persona_pending[-1].sent_at:
                ordered_persona = sorted(
                    (*self._persona_pending, message), key=lambda item: item.sent_at
                )
                self._persona_pending.clear()
                self._persona_pending.extend(ordered_persona)
            else:
                self._persona_pending.append(message)
            persona_cutoff = (
                message.sent_at - self.config.persona_increment_max_hours * 7200
            )
            while self._persona_pending and self._persona_pending[0].sent_at < persona_cutoff:
                self._persona_pending.popleft()
            while len(self._persona_pending) > 20_000:
                self._persona_pending.popleft()

    def _prune_group(self, group_id: str, now: float) -> None:
        """移除三小时前或超过配置条数的消息。"""
        queue = self._messages[group_id]
        cutoff = now - self.config.message_max_age_seconds
        while queue and queue[0].sent_at < cutoff:
            queue.popleft()
        while len(queue) > self.config.message_limit:
            queue.popleft()

    def messages(self, group_id: str, now: float) -> tuple[StreamMessage, ...]:
        """返回指定群当前可用于计算的只读消息快照。"""
        self._prune_group(str(group_id), now)
        return tuple(self._messages[str(group_id)])

    def has_bot_message(self, group_id: str, message_id: str, now: float) -> bool:
        """判断引用 ID 是否属于近三小时内机器人成功发送的消息。"""
        if not message_id:
            return False
        return any(
            item.is_bot and item.message_id == str(message_id)
            for item in self.messages(str(group_id), now)
        )

    def _group_activity(
        self, messages: tuple[StreamMessage, ...], now: float
    ) -> tuple[float, int, int, int]:
        """计算短期主导、长期辅助且考虑参与人数的群活跃度。"""
        human = [item for item in messages if not item.is_bot]
        short_cutoff = now - self.config.short_window_seconds
        recent_count = sum(item.sent_at >= short_cutoff for item in human)
        old_count = sum(item.sent_at < short_cutoff for item in human)
        recent_score = min(recent_count / self.config.short_full_messages, 1.0)
        old_score = min(old_count / self.config.old_full_messages, 1.0)
        participants = {
            item.user_id for item in human if item.sent_at >= short_cutoff
        }
        diversity = 0.7 + 0.3 * min(len(participants) / 6.0, 1.0)
        activity = min(1.0, (0.8 * recent_score + 0.2 * old_score) * diversity)
        return activity, recent_count, len(human), len(participants)

    def _stream_vector(
        self, messages: tuple[StreamMessage, ...], now: float
    ) -> dict[int, float]:
        """合并整个三小时消息流，最近十分钟消息拥有四倍权重。"""
        short_cutoff = now - self.config.short_window_seconds
        return average_vectors(
            (
                text_feature_vector(item.text),
                1.0 if item.sent_at >= short_cutoff else 0.25,
            )
            for item in messages
            if item.text
        )

    def _topic_matches(
        self, group_id: str, stream_vector: dict[int, float]
    ) -> tuple[TopicMatch, ...]:
        """从 SQLite 主题库选择与当前消息流最接近的三个话题。"""
        candidates: list[TopicMatch] = []
        for topic in self.store.list_willingness_topics(group_id, limit=500):
            vector = topic.get("features") or text_feature_vector(
                f"{topic.get('name', '')} {topic.get('summary', '')}"
            )
            similarity = cosine_similarity(stream_vector, vector)
            if similarity < 0.08:
                continue
            candidates.append(
                TopicMatch(
                    topic_id=str(topic["topic_id"]),
                    name=str(topic["name"]),
                    heat_level=str(topic.get("heat_level") or "peripheral"),
                    similarity=similarity,
                    familiarity=min(1.0, max(0.0, float(topic["familiarity"]))),
                    emotion_intensity=min(
                        1.0, max(0.0, float(topic.get("emotion_intensity") or 0))
                    ),
                    reply_count=max(0, int(topic.get("reply_count") or 0)),
                )
            )
        candidates.sort(
            key=lambda item: item.similarity * item.familiarity, reverse=True
        )
        return tuple(candidates[:3])

    def refresh_group(self, group_id: str, now: float, *, force: bool = False) -> EnvironmentSnapshot:
        """刷新一个群的环境快照；正常情况下同群五秒内最多计算一次。"""
        gid = str(group_id)
        cached = self._snapshots.get(gid)
        if (
            cached
            and not force
            and now - cached.updated_at < self.config.update_seconds
        ):
            return cached
        messages = self.messages(gid, now)
        activity, recent_count, total_human, participants = self._group_activity(
            messages, now
        )
        matches = self._topic_matches(gid, self._stream_vector(messages, now))
        heat_weights = {"core": 1.0, "secondary": 0.6, "peripheral": 0.3}
        weighted_familiarity = [
            (
                match.similarity
                * match.familiarity
                * heat_weights.get(match.heat_level, 0.3),
                match.similarity * heat_weights.get(match.heat_level, 0.3),
            )
            for match in matches
        ]
        familiarity_denominator = sum(weight for _, weight in weighted_familiarity)
        topic_familiarity = (
            sum(value for value, _ in weighted_familiarity) / familiarity_denominator
            if familiarity_denominator
            else 0.0
        )
        if matches:
            match_weights = [
                match.similarity * heat_weights.get(match.heat_level, 0.3)
                for match in matches
            ]
            match_total = sum(match_weights)
            emotion = sum(
                match.emotion_intensity * weight
                for match, weight in zip(matches, match_weights)
            ) / match_total
            engagement = sum(
                min(math.log1p(match.reply_count) / math.log1p(50), 1.0) * weight
                for match, weight in zip(matches, match_weights)
            ) / match_total
            fun_factor = 0.65 * emotion + 0.35 * engagement
        else:
            fun_factor = 0.0
        # 机器人刚发言后暂时降低群级意愿；后续新消息越多，恢复越快。
        cooldown_factor = 1.0
        last_reply = self._last_bot_reply_at.get(gid)
        if last_reply is not None:
            time_recovery = min(
                1.0,
                max(0.0, (now - last_reply) / self.config.reply_cooldown_seconds),
            )
            messages_after_reply = sum(
                not item.is_bot and item.sent_at > last_reply for item in messages
            )
            message_recovery = min(messages_after_reply / 4.0, 1.0)
            cooldown_factor = 0.35 + 0.65 * max(time_recovery, message_recovery)
            activity *= cooldown_factor
            topic_familiarity *= cooldown_factor
            fun_factor *= cooldown_factor
        # 每位近期参与者只取最后发言时刻，越新的关系对当前群氛围贡献越大。
        latest_by_speaker: dict[str, float] = {}
        for item in messages:
            if not item.is_bot and item.sent_at >= now - self.config.short_window_seconds:
                latest_by_speaker[item.user_id] = max(
                    item.sent_at, latest_by_speaker.get(item.user_id, 0.0)
                )
        recent_bond_values: list[tuple[float, float]] = []
        for recent_user_id, last_sent_at in latest_by_speaker.items():
            time_weight = max(
                0.05,
                1.0 - (now - last_sent_at) / self.config.short_window_seconds,
            )
            recent_bond_values.append(
                (
                    self.store.get_willingness_bond(
                        gid,
                        recent_user_id,
                        now,
                        grace_seconds=self.config.bond_grace_hours * 3600,
                        zero_seconds=self.config.bond_zero_days * 86400,
                    ),
                    time_weight,
                )
            )
        recent_bond_weight = sum(weight for _, weight in recent_bond_values)
        snapshot = EnvironmentSnapshot(
            updated_at=now,
            buffer_count=len(messages),
            recent_10m_count=recent_count,
            recent_3h_count=total_human,
            participant_count=participants,
            group_activity=activity,
            topic_familiarity=topic_familiarity,
            fun_factor=min(1.0, max(0.0, fun_factor)),
            recent_social_bond=(
                sum(value * weight for value, weight in recent_bond_values)
                / recent_bond_weight
                if recent_bond_weight
                else 0.0
            ),
            cooldown_factor=cooldown_factor,
            topic_matches=matches,
        )
        self._snapshots[gid] = snapshot
        return snapshot

    def refresh_all(self, now: float) -> None:
        """供五秒后台任务调用，仅刷新已有消息流，不产生控制台刷屏。"""
        for group_id in tuple(self._messages):
            self.refresh_group(group_id, now, force=True)

    def decide(
        self,
        group_id: str,
        user_id: str,
        text: str,
        *,
        mentioned: bool,
        within_work_hours: bool,
        local_date: str,
        now: float,
    ) -> WillingnessDecision:
        """基于最新群级快照执行一次八参数概率决策并立即打印日志。"""
        gid, uid = str(group_id), str(user_id)
        snapshot = self.refresh_group(gid, now)
        daily_replies = self.store.get_algorithm_reply_count(gid, local_date)
        current_bond = self.store.get_willingness_bond(
            gid,
            uid,
            now,
            grace_seconds=self.config.bond_grace_hours * 3600,
            zero_seconds=self.config.bond_zero_days * 86400,
        )
        persona = self.store.get_persona_profile(
            self.config.persona_user_id, self.config.personal_background
        )
        message_relevance = cosine_similarity(
            text_feature_vector(text), persona.get("features") or {}
        )
        features = WillingnessFeatures(
            user_activity=self.config.user_activity,
            group_activity=snapshot.group_activity,
            topic_familiarity=snapshot.topic_familiarity,
            social_bond=min(
                1.0, 0.6 * current_bond + 0.4 * snapshot.recent_social_bond
            ),
            is_mentioned=float(mentioned),
            message_relevance=message_relevance,
            fun_factor=snapshot.fun_factor,
            random_noise=self.rng.random(),
        )
        weights = self.config.weights()
        raw_score = sum(
            getattr(features, name) * weight for name, weight in weights.items()
        )
        probability = stable_sigmoid(
            self.config.sigmoid_k * (raw_score - self.config.sigmoid_midpoint)
        )
        random_draw = self.rng.random()

        # 即使被硬性门槛拦截也保留完整八参数，便于实时排障和复算。
        blocked_reason = ""
        if not self.config.enabled and not mentioned:
            blocked_reason = "主动回复已关闭"
        elif not text and not mentioned:
            blocked_reason = "没有可评分的文字内容"
        elif not within_work_hours:
            blocked_reason = "工作时段外"
        elif daily_replies >= self.config.daily_reply_limit:
            blocked_reason = "达到每群每日回复上限"
        accepted = not blocked_reason and random_draw < probability
        decision = WillingnessDecision(
            accepted=accepted,
            probability=probability,
            raw_score=raw_score,
            random_draw=random_draw,
            features=features,
            snapshot=snapshot,
            reason=(
                blocked_reason
                or ("概率抽样通过" if accepted else "概率抽样未通过")
            ),
            daily_replies=daily_replies,
            daily_limit=self.config.daily_reply_limit,
        )
        self._log_decision(gid, uid, decision, now)
        return decision

    def _blocked(
        self, snapshot: EnvironmentSnapshot, daily_replies: int, reason: str
    ) -> WillingnessDecision:
        """构造未进入概率计算时的统一决策对象。"""
        return WillingnessDecision(
            accepted=False,
            probability=0.0,
            raw_score=0.0,
            random_draw=0.0,
            features=None,
            snapshot=snapshot,
            reason=reason,
            daily_replies=daily_replies,
            daily_limit=self.config.daily_reply_limit,
        )

    def _log_decision(
        self, group_id: str, user_id: str, decision: WillingnessDecision, now: float
    ) -> None:
        """把每一次实际决策作为单行 JSON 实时输出到 CMD/PowerShell。"""
        weights = self.config.weights()
        feature_values = asdict(decision.features) if decision.features else None
        contributions = (
            {
                name: round(feature_values[name] * weights[name], 6)
                for name in weights
            }
            if feature_values
            else None
        )
        payload = {
            "event": "willingness_decision",
            "calculated_at": datetime.fromtimestamp(now).isoformat(),
            "group_id": group_id,
            "user_id": user_id,
            "buffer_count": decision.snapshot.buffer_count,
            "recent_10m_count": decision.snapshot.recent_10m_count,
            "recent_3h_count": decision.snapshot.recent_3h_count,
            "participant_count": decision.snapshot.participant_count,
            "cooldown_factor": round(decision.snapshot.cooldown_factor, 6),
            "features": (
                {name: round(value, 6) for name, value in feature_values.items()}
                if feature_values
                else None
            ),
            "weights": weights,
            "contributions": contributions,
            "topics": [
                {
                    "name": match.name,
                    "level": {
                        "core": "核心",
                        "secondary": "次要",
                        "peripheral": "边缘",
                    }.get(match.heat_level, "边缘"),
                    "level_code": match.heat_level,
                    "similarity": round(match.similarity, 6),
                    "familiarity": round(match.familiarity, 6),
                }
                for match in decision.snapshot.topic_matches
            ],
            "raw_score": round(decision.raw_score, 6),
            "probability": round(decision.probability, 6),
            "random_draw": round(decision.random_draw, 6),
            "will_reply": decision.accepted,
            "reason": decision.reason,
            "daily_replies": decision.daily_replies,
            "daily_limit": decision.daily_limit,
            "daily_remaining": max(0, decision.daily_limit - decision.daily_replies),
        }
        # 消息正文和个人背景绝不进入常规日志。
        logger.info("意愿计算 %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def record_inbound_interaction(
        self, group_id: str, user_id: str, now: float
    ) -> float:
        """记录群友 @/引用机器人，并返回更新后的关系值。"""
        return self.store.update_willingness_bond(
            group_id,
            user_id,
            now,
            self.config.bond_inbound_rate,
            grace_seconds=self.config.bond_grace_hours * 3600,
            zero_seconds=self.config.bond_zero_days * 86400,
            inbound=True,
        )

    def record_reply(
        self,
        group_id: str,
        user_id: str,
        local_date: str,
        now: float,
        *,
        text: str,
        message_id: str = "",
        display_name: str = "机器人",
    ) -> None:
        """成功发送算法回复后记额度、关系和机器人消息流。"""
        self.store.record_algorithm_reply(group_id, local_date, now)
        self.store.update_willingness_bond(
            group_id,
            user_id,
            now,
            self.config.bond_outbound_rate,
            grace_seconds=self.config.bond_grace_hours * 3600,
            zero_seconds=self.config.bond_zero_days * 86400,
            outbound=True,
        )
        self._last_bot_reply_at[str(group_id)] = now
        # 回复事件必须立即让下一条消息看到降温，不能等待旧快照自然满五秒。
        self._snapshots.pop(str(group_id), None)
        self.record_message(
            StreamMessage(
                group_id=str(group_id),
                user_id="bot",
                display_name=display_name,
                sent_at=now,
                text=text,
                message_id=str(message_id or ""),
                is_bot=True,
            )
        )

    def analysis_interval_hours(self, group_id: str, now: float) -> float:
        """由过去三小时密度线性映射得到 3–10 小时分析间隔。"""
        activity = self.refresh_group(group_id, now).group_activity
        span = self.config.topic_analysis_max_hours - self.config.topic_analysis_min_hours
        return min(
            self.config.topic_analysis_max_hours,
            max(
                self.config.topic_analysis_min_hours,
                self.config.topic_analysis_max_hours - span * activity,
            ),
        )

    def anonymized_transcript(self, group_id: str, now: float) -> list[dict]:
        """为主题模型生成批次内匿名、按时间排序的消息列表。"""
        aliases: dict[str, str] = {}
        output: list[dict] = []
        for item in self.messages(group_id, now):
            if item.is_bot:
                speaker = "机器人"
            else:
                speaker = aliases.setdefault(
                    item.user_id, f"成员{len(aliases) + 1}"
                )
            if item.text:
                output.append(
                    {
                        "speaker": speaker,
                        "time": datetime.fromtimestamp(item.sent_at).isoformat(),
                        "text": item.text[:500],
                    }
                )
        return output

    async def analyze_due_groups(self, analyzer, now: float) -> None:
        """分析所有到期群；单群失败只记录状态，不破坏已有主题知识。"""
        for group_id in tuple(self._messages):
            if group_id in self._analysis_running:
                continue
            state = self.store.get_willingness_analysis_state(group_id)
            interval = self.analysis_interval_hours(group_id, now)
            last_attempt = float(state.get("last_attempt_at") or 0)
            stored_next = float(state.get("next_analysis_at") or 0)
            due_at = stored_next or (last_attempt + interval * 3600 if last_attempt else 0)
            if now < due_at or not self.anonymized_transcript(group_id, now):
                continue
            self._analysis_running.add(group_id)
            # 先写入三小时失败退避；成功后再覆盖为按活跃度计算的 3–10 小时。
            retry_at = now + self.config.topic_analysis_min_hours * 3600
            self.store.mark_willingness_analysis_attempt(group_id, now, retry_at)
            try:
                topics = await analyzer.analyze_willingness_topics(
                    self.anonymized_transcript(group_id, now), now
                )
                public_topics = [
                    topic
                    for topic in topics
                    if topic.get("public_query")
                    and not re.search(
                        r"成员\d+|QQ\s*\d+|(?<!\d)\d{5,}(?!\d)",
                        str(topic.get("public_query")),
                        re.IGNORECASE,
                    )
                ]
                enrichment = {}
                if public_topics:
                    try:
                        by_query = await analyzer.enrich_willingness_topics(
                            [str(topic["public_query"]) for topic in public_topics], now
                        )
                        # 存储层以规范化话题名为键，联网客户端则按原检索词返回。
                        enrichment = {
                            str(topic.get("name") or ""): str(
                                by_query.get(str(topic["public_query"])) or ""
                            )
                            for topic in public_topics
                        }
                    except Exception:  # noqa: BLE001
                        logger.exception("群 %s 话题联网丰富失败，保留本地提取结果", group_id)
                self.store.save_willingness_topics(
                    group_id, topics, enrichment, now
                )
                next_at = now + self.analysis_interval_hours(group_id, now) * 3600
                self.store.mark_willingness_analysis_success(group_id, now, next_at)
            except Exception:  # noqa: BLE001
                logger.exception("群 %s 意愿话题分析失败，旧知识保持不变", group_id)
            finally:
                self._analysis_running.discard(group_id)

        # 个人背景按所有群的新发言合并更新一次，避免先处理的群推进游标后漏掉其他群。
        if self.config.persona_user_id:
            persona_state = self.store.get_persona_profile(
                self.config.persona_user_id, self.config.personal_background
            )
            processed_at = float(persona_state.get("last_source_message_at") or 0)
            profile_updated_at = float(persona_state.get("updated_at") or 0)
            # 使用人格专属队列而不是三小时群流，低活跃时也能满足 24 小时更新策略。
            persona_items = sorted(
                (item for item in self._persona_pending if item.sent_at > processed_at),
                key=lambda item: item.sent_at,
            )
            enough_volume = len(persona_items) >= self.config.persona_increment_min_messages
            due_by_age = (
                len(persona_items) >= self.config.persona_increment_floor_messages
                and now - profile_updated_at
                >= self.config.persona_increment_max_hours * 3600
            )
            if enough_volume or due_by_age:
                try:
                    # 单批最多 200 条，确保不会因模型上下文上限跳过后仍错误推进游标。
                    analysis_items = persona_items[:200]
                    profile = await analyzer.analyze_persona(
                        [item.text for item in analysis_items],
                        {},
                        self.config.personal_background,
                        now,
                    )
                    # 已激活的结构化画像作为历史证据参与合并；少量新消息不会覆盖旧人格。
                    if persona_state.get("active_version") and persona_state.get("dimensions"):
                        profile = merge_persona_profiles(
                            [persona_state, profile],
                            group_weight=self.config.persona_group_style_weight,
                        )
                    # 存储层使用真实最新源消息时间，而不是信任模型生成游标。
                    profile["last_source_message_at"] = analysis_items[-1].sent_at
                    self.store.save_persona_version(
                        self.config.persona_user_id,
                        profile,
                        now,
                        # 已有经确认版本时才自动激活渐进更新；首次画像保持草稿。
                        activate=bool(persona_state.get("active_version")),
                    )
                    processed_at = analysis_items[-1].sent_at
                    while self._persona_pending and self._persona_pending[0].sent_at <= processed_at:
                        self._persona_pending.popleft()
                except Exception:  # noqa: BLE001
                    logger.exception("意愿模块个人背景增量更新失败，继续使用旧背景")
