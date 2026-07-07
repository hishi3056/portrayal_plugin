"""NapCat API 消息采集器 — 直接从 QQ 服务器拉群聊历史。

特性：
  - 多轮翻页拉取（reverseOrder=True + message_id 游标实现向前回溯）
  - 群级游标共享（同群查多人时复用扫描进度）
  - 用户级缓存（TTL 10分钟，同群查多人时复用）
  - OneBot 消息段解析（text + at 段提取）
  - 防御性解析（isinstance + try/except）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CollectedMessage:
    """单条已解析的消息。"""
    user_id: str
    user_name: str
    text: str
    timestamp: float
    message_id: str


@dataclass
class CollectionResult:
    """采集结果。"""
    target_messages: list[CollectedMessage] = field(default_factory=list)
    all_messages: list[CollectedMessage] = field(default_factory=list)
    total_scanned: int = 0
    api_calls: int = 0
    from_cache: bool = False
    error: str = ""

    @property
    def target_count(self) -> int:
        return len(self.target_messages)

    @property
    def is_empty(self) -> bool:
        return not self.target_messages


@dataclass
class _CachedPage:
    """缓存的单页消息。"""
    messages: list[CollectedMessage]
    timestamp: float


class NapCatCollector:
    """NapCat API 消息采集器。

    通过 MaiBot 插件 SDK 的 api.call 调用 NapCat adapter 的
    get_group_msg_history API，直接从 QQ 服务器拉群聊历史。
    """

    def __init__(
        self,
        *,
        per_query_count: int = 200,
        max_rounds: int = 10,
        max_msg_count: int = 100,
        scan_hours: int = 168,
        cache_ttl_seconds: int = 600,
    ):
        self.per_query_count = per_query_count
        self.max_rounds = max_rounds
        self.max_msg_count = max_msg_count
        self.scan_hours = scan_hours
        self.cache_ttl = cache_ttl_seconds

        # 用户级缓存: group_id:user_id -> list[CollectedMessage]
        self._user_cache: dict[str, list[CollectedMessage]] = {}
        self._cache_timestamps: dict[str, float] = {}

        # 群级缓存: group_id -> list[CollectedMessage]（全量消息，复用做群分析）
        self._group_cache: dict[str, list[CollectedMessage]] = {}
        self._group_cache_ts: dict[str, float] = {}

        # 群级游标: group_id -> message_id (扫描断点)
        self._group_cursor: dict[str, str] = {}

    # ─── 公开接口 ──────────────────────────────────────────────────

    async def collect(
        self,
        api_call_fn: Any,
        group_id: str,
        target_user_id: str,
    ) -> CollectionResult:
        """采集目标用户在指定群的历史消息。

        Args:
            api_call_fn: 可调用对象，用于调 self.ctx.api.call()
            group_id: QQ 群号
            target_user_id: 目标用户 QQ 号

        Returns:
            CollectionResult: 采集结果
        """
        now = time.time()
        cutoff = now - self.scan_hours * 3600

        # 1. 检查缓存
        cache_key = f"{group_id}:{target_user_id}"
        cached = self._get_cache(cache_key)
        if cached and len(cached) >= self.max_msg_count:
            return CollectionResult(
                target_messages=cached[:self.max_msg_count],
                all_messages=cached[:self.max_msg_count],
                total_scanned=len(cached),
                api_calls=0,
                from_cache=True,
            )

        # 已采集消息的去重集合
        seen_message_ids: set[str] = set()
        target_messages: list[CollectedMessage] = []
        all_collected: list[CollectedMessage] = []

        # 从缓存初始化（如果有）
        if cached:
            for m in cached:
                if m.message_id and m.message_id not in seen_message_ids:
                    seen_message_ids.add(m.message_id)
                    target_messages.append(m)
                    all_collected.append(m)

        # 确定起始游标：用群级游标继续往前扫，否则从最新开始
        # 参考 astrbot_plugin_portrayal: reverseOrder=True + messages[0].message_id 做游标
        message_seq = self._group_cursor.get(group_id, 0)

        total_scanned = 0
        error_msg = ""
        api_failures = 0
        rounds = 0
        self._last_error = ""

        # 2. 多轮翻页拉取（reverseOrder=True 实现向前回溯）
        while rounds < self.max_rounds and len(target_messages) < self.max_msg_count:
            try:
                result = await api_call_fn(
                    "adapter.napcat.message.get_group_msg_history",
                    version="1",
                    params={
                        "group_id": int(group_id),
                        "message_seq": message_seq,
                        "count": self.per_query_count,
                        "reverseOrder": True,
                    },
                )
                api_failures = 0  # 重置失败计数
            except Exception as exc:
                api_failures += 1
                error_msg = str(exc)
                self._last_error = error_msg
                if api_failures >= 2:
                    break
                import asyncio
                await asyncio.sleep(1)
                continue

            if not isinstance(result, dict):
                error_msg = "API 返回非 dict"
                break

            # NapCat OneBot 动作返回 {status, data: {messages: [...]}}
            raw_data: dict = result.get("data", result) if isinstance(result, dict) else {}
            if isinstance(raw_data, dict):
                raw_messages = raw_data.get("messages", [])
            else:
                raw_messages = []
            if not raw_messages or not isinstance(raw_messages, list):
                self._last_error = f"API 返回空消息列表 (round {rounds+1})"
                break

            self._last_error = ""
            # 解析消息
            parsed: list[CollectedMessage] = []
            for raw_msg in raw_messages:
                if not isinstance(raw_msg, dict):
                    continue
                msg = self._parse_onebot_message(raw_msg)
                if msg:
                    parsed.append(msg)

            if not parsed:
                break

            # reverseOrder=True 时 messages[0] 是最老的，用它做下一轮游标
            # 用原始 raw_messages[0] 的 message_id（不转 int，直接传给 NapCat）
            first_raw = raw_messages[0] if isinstance(raw_messages[0], dict) else {}
            next_seq = str(first_raw.get("message_id", "") or "")
            if next_seq and next_seq != str(message_seq):
                self._group_cursor[group_id] = next_seq
                message_seq = next_seq
            else:
                # 游标没变，说明到底了
                break

            # 显式按时间戳排序
            parsed.sort(key=lambda m: m.timestamp)

            total_scanned += len(parsed)

            # debug: 打印每轮拉取情况
            target_in_round = sum(1 for m in parsed if m.user_id == target_user_id)
            self._last_error = f"round {rounds+1}: raw={len(raw_messages)} parsed={len(parsed)} target={target_in_round}"

            # 去重
            new_parsed: list[CollectedMessage] = []
            for m in parsed:
                if m.message_id and m.message_id in seen_message_ids:
                    continue
                if m.message_id:
                    seen_message_ids.add(m.message_id)
                new_parsed.append(m)

            if not new_parsed:
                break

            parsed = new_parsed

            # 时间边界检查：reverseOrder=True 返回最老→最新顺序
            # 需判断整个批次的相对位置，而非仅看最旧的一条
            newest = parsed[-1]  # 最新消息
            oldest = parsed[0]   # 最旧消息

            if newest.timestamp < cutoff:
                # 整批消息都在截止时间之前，跳过但继续翻页
                self._distribute_cache(group_id, parsed)
                rounds += 1
                continue

            if oldest.timestamp < cutoff:
                # 部分在截止时间之前，过滤掉旧消息
                parsed = [m for m in parsed if m.timestamp >= cutoff]

            # 分发缓存
            self._distribute_cache(group_id, parsed)

            # 提取目标用户
            for m in parsed:
                if m.user_id == target_user_id:
                    target_messages.append(m)
                all_collected.append(m)

            # 如果最旧的消息已在截止范围内，说明已扫到了有效区间
            # 继续往前翻可能还有更多（reverseOrder 下越往后越新）
            rounds += 1

        # 更新目标用户缓存
        self._set_cache(cache_key, target_messages)

        return CollectionResult(
            target_messages=target_messages[:self.max_msg_count],
            all_messages=all_collected,
            total_scanned=total_scanned,
            api_calls=rounds if total_scanned > 0 else 0,
            from_cache=bool(cached) and total_scanned == 0,
            error=error_msg or (getattr(self, '_last_error', '') if not target_messages else ''),
        )

    # ─── OneBot 消息解析 ───────────────────────────────────────────

    @staticmethod
    def _parse_onebot_message(raw: dict) -> CollectedMessage | None:
        """解析单条 OneBot 格式消息。

        NapCat 返回的消息结构：
        {
            "message_id": "...",
            "sender": {"user_id": 123, "nickname": "...", "card": "..."},
            "message": [{"type": "text", "data": {"text": "..."}}, ...],
            "time": 1234567890
        }
        """
        try:
            # 发送者信息
            sender = raw.get("sender", {})
            if not isinstance(sender, dict):
                return None
            user_id = str(sender.get("user_id", "") or "").strip()
            if not user_id:
                return None
            user_name = str(
                sender.get("card") or sender.get("nickname") or user_id
            )

            # 消息段
            segments = raw.get("message", [])
            if not isinstance(segments, list):
                return None

            text_parts: list[str] = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type", "")
                seg_data = seg.get("data", {})
                if not isinstance(seg_data, dict):
                    continue

                if seg_type == "text":
                    text_parts.append(str(seg_data.get("text", "") or ""))
                elif seg_type == "at":
                    # MaiBot AtComponent 序列化为 {"type":"at","data":{"target_user_id":"..."}}
                    qq = str(seg_data.get("target_user_id", "") or seg_data.get("qq", "") or "")
                    if qq:
                        text_parts.append(f"@{qq}")

            text = "".join(text_parts).strip()
            if not text:
                return None

            # 时间戳
            timestamp = float(raw.get("time", 0) or 0)

            # 消息 ID
            message_id = str(raw.get("message_id", "") or "")

            return CollectedMessage(
                user_id=user_id,
                user_name=user_name,
                text=text,
                timestamp=timestamp,
                message_id=message_id,
            )
        except Exception:
            return None

    # ─── 缓存管理 ──────────────────────────────────────────────────

    def _distribute_cache(self, group_id: str, messages: list[CollectedMessage]) -> None:
        """将一页消息按 user_id 分发到用户缓存，同时追加到群缓存。"""
        now = time.time()
        for msg in messages:
            key = f"{group_id}:{msg.user_id}"
            cached = self._user_cache.get(key)
            if cached is None:
                self._user_cache[key] = [msg]
            else:
                cached.append(msg)
            self._cache_timestamps[key] = now
        # 群缓存
        gc = self._group_cache.get(group_id)
        if gc is None:
            self._group_cache[group_id] = list(messages)
        else:
            gc.extend(messages)
        self._group_cache_ts[group_id] = now

    def _get_cache(self, key: str) -> list[CollectedMessage] | None:
        """获取缓存，过期则清除。"""
        if key not in self._user_cache:
            return None
        ts = self._cache_timestamps.get(key, 0)
        if time.time() - ts > self.cache_ttl:
            del self._user_cache[key]
            self._cache_timestamps.pop(key, None)
            return None
        return self._user_cache[key]

    def _set_cache(self, key: str, messages: list[CollectedMessage]) -> None:
        """设置缓存。"""
        self._user_cache[key] = messages
        self._cache_timestamps[key] = time.time()

    def clear_cache(self) -> None:
        """清空所有缓存。"""
        self._user_cache.clear()
        self._cache_timestamps.clear()
        self._group_cache.clear()
        self._group_cache_ts.clear()

    def get_group_messages(self, group_id: str) -> list[CollectedMessage] | None:
        """获取群级缓存的消息列表（用于群分析复用）。过期返回 None。"""
        if group_id not in self._group_cache:
            return None
        ts = self._group_cache_ts.get(group_id, 0)
        if time.time() - ts > self.cache_ttl:
            del self._group_cache[group_id]
            self._group_cache_ts.pop(group_id, None)
            return None
        return self._group_cache[group_id]
