"""群消息分析器 — 统计 + LLM 标签生成。

复用画像采集时拉取的全群消息，做：
1. 规则统计（零 token）：高频词、用户口头禅、@ 关系图
2. LLM 标签（~700 tokens）：群标签、话题命名、一句话总结
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any, Optional

import aiohttp


class GroupAnalyzer:
    """群消息分析器。"""

    def __init__(
        self,
        *,
        deepseek_api_key: str = "",
        deepseek_base_url: str = "https://api.deepseek.com/v1",
        deepseek_model: str = "deepseek-chat",
        temperature: float = 0.3,
    ):
        self._api_key = deepseek_api_key
        self._base_url = deepseek_base_url.rstrip("/")
        self._model = deepseek_model
        self._temperature = temperature

    # ─── 统计层（纯规则，零 token）─────────────────────────────────

    @staticmethod
    def compute_stats(messages: list, group_id: str) -> dict:
        """计算群聊统计数据。

        Args:
            messages: list[CollectedMessage]，全群消息
            group_id: 群号

        Returns:
            dict with: top_words, user_catchphrases, user_relations,
                       message_count, user_count, sample_text
        """
        if not messages:
            return {
                "top_words": [],
                "user_catchphrases": {},
                "user_relations": {},
                "message_count": 0,
                "user_count": 0,
                "sample_text": "",
            }

        # 按用户分组（过滤 bot 系统消息和画像输出文本）
        _bot_markers = ('正在扫描', '已扫描', '画像已保存', '正在生成', '正在采集',
                       '【性格标签】', '【优势特质】', '稳定偏好', '表达风格',
                       '性格标签', '优势特质', '相处建议', '语言风格')
        user_msgs: dict[str, list[str]] = {}
        for m in messages:
            uid = m.user_id
            text = m.text
            if any(p in text for p in _bot_markers):
                continue
            if uid not in user_msgs:
                user_msgs[uid] = []
            user_msgs[uid].append(text)

        # 高频词统计
        word_counter = Counter()
        cn_pattern = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}|\d{4,}")
        for uid, texts in user_msgs.items():
            for text in texts:
                for match in cn_pattern.finditer(text):
                    word_counter[match.group()] += 1

        # 过滤停用词
        stop_words = {
            "因为", "所以", "但是", "而且", "不过", "然后", "可以", "应该",
            "这个", "那个", "什么", "怎么", "这么", "那么", "如果", "虽然",
            "已经", "还是", "或者", "就是", "没有", "不是", "不要", "不能",
            "知道", "觉得", "可能", "但是", "只是", "真的", "好吧", "好的",
            "哈哈哈哈", "哈哈", "啊啊", "嗯嗯",
        }
        top_words = [
            {"word": w, "count": c}
            for w, c in word_counter.most_common(50)
            if w not in stop_words
        ][:30]

        # 用户口头禅（ChatLab 式：整条消息频率统计）
        user_catchphrases: dict[str, list] = {}
        _media_set = {"[图片]","[视频]","[语音]","[文件]","[动画表情]","[表情]","[链接]","[位置]","[红包]","[转账]","[音乐]","[回复消息]"}
        for uid, texts in user_msgs.items():
            msg_counter = Counter()
            for text in texts:
                raw = text.strip()
                if len(raw) >= 2 and raw not in _media_set and not raw.startswith(('+', '/', '!', '#')):
                    msg_counter[raw] += 1
            phrases = [
                {"phrase": p, "count": c}
                for p, c in msg_counter.most_common(20)
                if len(p) >= 2 and re.search(r"[\u4e00-\u9fff]", p)
            ][:5]
            if phrases:
                user_catchphrases[uid] = phrases

        # 用户关系图（消息相邻对 + @ 关系）
        user_relations: dict[str, dict[str, int]] = {}
        for i in range(len(messages) - 1):
            a = messages[i].user_id
            b = messages[i+1].user_id
            if a == b:
                continue
            if a not in user_relations:
                user_relations[a] = {}
            user_relations[a][b] = user_relations[a].get(b, 0) + 1

        # @ 关系（从消息文本中解析 @123456）
        at_pattern = re.compile(r"@(\d{5,12})")
        for m in messages:
            for at_uid in at_pattern.findall(m.text):
                if at_uid != m.user_id:
                    if m.user_id not in user_relations:
                        user_relations[m.user_id] = {}
                    user_relations[m.user_id][at_uid] = user_relations[m.user_id].get(at_uid, 0) + 1

        # 只保留 Top 关系
        for uid in list(user_relations.keys()):
            sorted_r = sorted(user_relations[uid].items(), key=lambda x: -x[1])[:10]
            user_relations[uid] = {k: v for k, v in sorted_r}

        # 样例文本（供 LLM 参考）
        sample_texts = [m.text for m in messages[-100:]]
        sample_text = "\n".join(sample_texts[:30])[:1500]

        return {
            "top_words": top_words,
            "user_catchphrases": user_catchphrases,
            "user_relations": user_relations,
            "message_count": len(messages),
            "user_count": len(user_msgs),
            "sample_text": sample_text,
        }

    # ─── LLM 标签生成 ──────────────────────────────────────────────

    async def generate_tags(self, stats: dict) -> dict:
        """根据统计数据生成群标签和话题名。

        Args:
            stats: compute_stats 的输出

        Returns:
            dict with: tags, hot_topics, summary
        """
        # 构建轻量 prompt
        top_words_str = ", ".join([w["word"] for w in stats.get("top_words", [])[:20]])
        sample = stats.get("sample_text", "")[:800]
        user_count = stats.get("user_count", 0)
        msg_count = stats.get("message_count", 0)

        system = (
            "你是一个群聊分析助手。根据提供的统计信息，输出群聊分析结果。\n"
            "输出格式为 JSON，只输出 JSON，不要其他文字：\n"
            '{"tags":["标签1","标签2/子标签",...],"hot_topics":[{"name":"话题名","keywords":["词1","词2"],"weight":100}],'
            '"summary":"一句话总结"}\n\n'
            "标签格式：大类/小类，如 游戏/原神、技术/编程、动漫、生活\n"
            "话题 weight 表示提及频率（100 为最高）\n"
            "一句话总结不超过 50 字"
        )

        user_prompt = (
            f"群聊统计：{user_count} 人参与，共 {msg_count} 条消息。\n"
            f"高频词汇（前20）：{top_words_str}\n"
            f"最近消息样例：\n{sample}\n\n"
            "请分析这个群的标签、热门话题，写一句话总结。"
        )

        try:
            raw = await self._call_llm(system, user_prompt)
            # 尝试解析 JSON
            # 去掉可能的 markdown 代码块标记
            raw = re.sub(r"```(?:json)?\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw).strip()
            result = json.loads(raw)
            return {
                "tags": result.get("tags", []),
                "hot_topics": result.get("hot_topics", []),
                "summary": result.get("summary", ""),
            }
        except Exception as exc:
            # LLM 失败时返回纯统计标签
            return {
                "tags": [],
                "hot_topics": [
                    {"name": w["word"], "keywords": [w["word"]], "weight": min(w["count"] * 10, 100)}
                    for w in stats.get("top_words", [])[:5]
                ],
                "summary": f"共 {stats.get('user_count', 0)} 人参与 {stats.get('message_count', 0)} 条消息讨论",
            }

    async def _call_llm(self, system: str, user: str) -> str:
        """调 Deepseek API。"""
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._temperature,
            "max_tokens": 300,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                data = await resp.json()
                if "choices" in data and len(data["choices"]) > 0:
                    return data["choices"][0]["message"]["content"]
                raise RuntimeError(f"LLM 返回异常: {data}")

    # ─── 完整分析流程 ──────────────────────────────────────────────

    async def analyze(
        self, messages: list, group_id: str, *, skip_llm: bool = False
    ) -> dict:
        """完整群分析流程。"""
        stats = self.compute_stats(messages, group_id)
        if skip_llm:
            tags_result = {
                "tags": [],
                "hot_topics": [],
                "summary": "",
            }
        else:
            tags_result = await self.generate_tags(stats)
        return {**stats, **tags_result}
