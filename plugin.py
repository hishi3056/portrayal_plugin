"""人物画像插件 v0.4.0 - NapCat API 采集 + 独立存储 + hook 注入 + WebUI。

指令:
  画像 [可选@/名字]      全面分析目标用户
  阴阳画像 [可选@/名字]   毒舌风格分析
  查看画像 [可选@/名字]   查看已生成的画像

存储:独立 SQLite(store.py),版本历史 + 群维度 + 激活/停用
采集:NapCat API 直调(napcat_collector.py),绕过 MaiBot 消息存储
注入:@HookHandler("maisaka.replyer.before_request") 覆盖 reply_tool_args
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import MaiBotPlugin, Command, PluginConfigBase, HookHandler, Field
from maibot_sdk.types import HookMode, HookOrder, ErrorPolicy

from .store import ProfileDB, ProfileRecord, GroupAnalysisRecord
from .napcat_collector import NapCatCollector, CollectedMessage, CollectionResult
from .web_server import PortrayalWebServer
from .group_analyzer import GroupAnalyzer
import re as _re
import json as _json
from collections import Counter as _Counter


# ═══════════════════════════════════════════════════════════════════════
#  Prompt 模板(外置到 prompts/ 目录,启动时加载)
# ═══════════════════════════════════════════════════════════════════════

# 默认 prompt(文件读取失败时兜底)
_DEFAULT_PORTRAIT_SYSTEM = "你是一个人物观察者。请根据群聊记录分析用户的稳定特征。"
_DEFAULT_YIN_YANG_SYSTEM = "你是一个毒舌但准确的人物观察者。请根据群聊记录用犀利幽默的方式分析用户。"
_DEFAULT_PORTRAIT_USER = "请分析目标用户「{target_name}」的特征。\n\n聊天记录:\n{chat_log}\n\n已有画像:\n{existing_profile}\n\n日期:{date} 消息数:{msg_count} 群:{group_id}"
_DEFAULT_YIN_YANG_USER = "请用毒舌风格分析目标用户「{target_name}」的特征。\n\n聊天记录:\n{chat_log}\n\n已有画像:\n{existing_profile}\n\n日期:{date} 消息数:{msg_count} 群:{group_id}"


# ═══════════════════════════════════════════════════════════════════════
#  配置模型
# ═══════════════════════════════════════════════════════════════════════

OVERRIDE_MAX_CHARS = 800
MIN_TARGET_MSGS = 5


class PluginSectionConfig(PluginConfigBase):
    """[plugin] 节"""
    config_version: str = Field(default="1.0.0", description="配置版本")
    enabled: bool = Field(default=True, description="是否启用插件")


class PortrayalSectionConfig(PluginConfigBase):
    """[portrayal] 节"""
    scan_hours: int = Field(default=168, description="消息采集时间范围(小时),默认7天")
    message_limit: int = Field(default=100, description="目标用户发言采集上限")
    refresh_remind_days: int = Field(default=14, description="画像刷新提醒天数")
    # ─── LLM 配置 ──────────────────────────────────────────────────
    provider: str = Field(default="deepseek", description="LLM 供应商: deepseek | maibot")
    deepseek_api_key: str = Field(default="", description="Deepseek API Key")
    deepseek_base_url: str = Field(default="https://api.deepseek.com/v1", description="Deepseek API 地址")
    deepseek_model: str = Field(default="deepseek-chat", description="Deepseek 模型名")
    temperature: float = Field(default=0.7, description="LLM 温度")
    chat_log_max_chars: int = Field(default=8000, description="聊天记录截断字符数")
    profile_ttl_days: int = Field(default=0, description="画像 TTL 天数,0=永不过期")
    webui_port: int = Field(default=8089, description="WebUI 端口(v0.4.0 启用)")
    webui_host: str = Field(default="127.0.0.1", description="WebUI 监听地址,默认仅本机;如需局域网访问改 0.0.0.0 并务必设置 webui_token")
    webui_token: str = Field(default="", description="WebUI 访问 token,空=无认证;非本机绑定时强烈建议设置")
    per_query_count: int = Field(default=200, description="每次 NapCat API 调用拉取条数")
    max_rounds: int = Field(default=10, description="最大 API 调用轮数(10轮=最多2000条全群消息)")
    cache_ttl_minutes: int = Field(default=10, description="消息缓存 TTL(分钟)")
    enable_injection: bool = Field(default=True, description="是否通过 Hook 注入画像到 Replyer(需 MaiSaka 补丁或 MaiBot 1.0.6+;关闭后不影响画像采集、WebUI 等其他功能;原版 Planner 注入由 MaiBot 全局配置控制,与本开关无关)")
    enable_planner_injection: bool = Field(default=False, description="是否通过 Hook 注入画像到 Planner,替换原版 A_memorix 画像(需 MaiBot 1.0.6+;默认关闭;开启后建议在 MaiBot 全局配置中关闭自动注入人物画像以避免重复查询)")
    enable_cross_group_merge: bool = Field(default=True, description="跨群画像聚合:多群画像合并注入")
    llm_retry_times: int = Field(default=1, description="LLM 调用失败重试次数")
    min_target_messages: int = Field(default=3, description="目标用户最低文本发言条数,低于此值拒绝生成画像")
    protected_user_ids: list[str] = Field(default_factory=list, description="保护名单,不允许被分析")
    allowed_user_ids: list[str] = Field(default_factory=list, description="触发权限白名单,空=所有人可用")
    command_prefix: str = Field(default="+", description="命令前缀,默认+")
    enable_image_output: bool = Field(default=True, description="是否渲染图片展示版")


class PortrayalConfig(PluginConfigBase):
    """人物画像插件配置模型。"""
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    portrayal: PortrayalSectionConfig = Field(default_factory=PortrayalSectionConfig)


# ═══════════════════════════════════════════════════════════════════════
#  插件主体
# ═══════════════════════════════════════════════════════════════════════


class PortrayalPlugin(MaiBotPlugin):
    config_model = PortrayalConfig

    # ─── 生命周期 ────────────────────────────────────────────────────

    async def on_load(self) -> None:
        self._db: ProfileDB = ProfileDB(self._get_db_path())
        self._collector: NapCatCollector = NapCatCollector(
            per_query_count=self.config.portrayal.per_query_count,
            max_rounds=self.config.portrayal.max_rounds,
            max_msg_count=self.config.portrayal.message_limit,
            scan_hours=self.config.portrayal.scan_hours,
            cache_ttl_seconds=self.config.portrayal.cache_ttl_minutes * 60,
        )
        self.ctx.logger.info("人物画像插件 v0.3.1 已加载,DB: %s", self._db.db_path)
        self._prompts: dict[str, str] = {}
        self._prompt_mtimes: dict[str, float] = {}
        self._last_usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._load_prompts()
        self._web: PortrayalWebServer | None = None
        self._group_analyzer: GroupAnalyzer | None = None
        self._start_web()

    async def on_unload(self) -> None:
        await self._stop_web()
        self.ctx.logger.info("人物画像插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("人物画像插件配置已更新: version=%s", version)
            self._load_prompts()
            await self._stop_web()
            self._start_web()

    def _start_web(self) -> None:
        """启动 WebUI 服务器。"""
        try:
            self._web = PortrayalWebServer(
                db=self.db,
                prompts_dir=self._get_prompts_dir(),
                prompts=self._prompts,
                logger=self.ctx.logger,
                port=self.config.portrayal.webui_port,
                host=self.config.portrayal.webui_host,
                auth_token=self.config.portrayal.webui_token,
                image_cache_dir=self._get_image_cache_dir(),
                collect_callback=self._webui_collect,
                search_friends_callback=self._search_friends,
            )
            task = asyncio.create_task(self._web.start())
            task.add_done_callback(
                lambda t: self.ctx.logger.error("WebUI 启动异常: %s", t.exception())
                if t.exception() else None
            )
            self.ctx.logger.info("WebUI 正在启动: http://127.0.0.1:%d", self.config.portrayal.webui_port)
        except Exception as exc:
            self.ctx.logger.warning("WebUI 启动失败: %s", exc)
            self._web = None

    async def _stop_web(self) -> None:
        if self._web is not None:
            await self._web.stop()
            self._web = None

    def _get_prompts_dir(self) -> Path:
        try:
            return self.ctx.paths.plugin_dir / "prompts"
        except Exception:
            return Path(__file__).parent / "prompts"

    def _load_prompts(self) -> None:
        """从 prompts/ 目录加载外置 prompt 模板,失败时用默认值。

        记录文件修改时间,_check_prompts_hot_reload() 用它判断是否需要重载。
        """
        prompts_dir = self._get_prompts_dir()
        self._prompts.clear()
        self._prompt_mtimes = {}
        files = {
            "portrait_system": ("portrait_system.txt", _DEFAULT_PORTRAIT_SYSTEM),
            "portrait_user": ("portrait_user.txt", _DEFAULT_PORTRAIT_USER),
            "yin_yang_system": ("yin_yang_system.txt", _DEFAULT_YIN_YANG_SYSTEM),
            "yin_yang_user": ("yin_yang_user.txt", _DEFAULT_YIN_YANG_USER),
        }
        for key, (filename, fallback) in files.items():
            try:
                path = prompts_dir / filename
                if path.exists():
                    self._prompts[key] = path.read_text(encoding="utf-8").strip()
                    self._prompt_mtimes[filename] = path.stat().st_mtime
                    self.ctx.logger.debug("已加载 prompt: %s", filename)
                else:
                    self._prompts[key] = fallback
                    self.ctx.logger.warning("prompt 文件不存在,使用默认值: %s", filename)
            except Exception as exc:
                self._prompts[key] = fallback
                self.ctx.logger.warning("加载 prompt 失败: %s err=%s", filename, exc)

    def _check_prompts_hot_reload(self) -> bool:
        """检查 prompt 文件是否有变更,有则热重载。返回 True 表示重载了。"""
        prompts_dir = self._get_prompts_dir()
        changed = False
        for filename in list(self._prompt_mtimes.keys()):
            path = prompts_dir / filename
            try:
                if not path.exists():
                    continue
                mtime = path.stat().st_mtime
                if mtime != self._prompt_mtimes.get(filename):
                    changed = True
                    break
            except Exception:
                pass
        if not changed:
            # 也要检查新文件
            files = {"portrait_system.txt", "portrait_user.txt", "yin_yang_system.txt", "yin_yang_user.txt"}
            for filename in files:
                path = prompts_dir / filename
                if path.exists() and filename not in self._prompt_mtimes:
                    changed = True
                    break
        if changed:
            self.ctx.logger.info("检测到 prompt 文件变更,热重载中...")
            self._load_prompts()
            # 同步到 WebServer
            if self._web:
                self._web._prompts = self._prompts
            return True
        return False

    def _get_db_path(self) -> Path:
        try:
            return self.ctx.paths.data_dir / "portrayal.db"
        except Exception:
            return Path("data/plugins/rc.portrayal/portrayal.db")

    def _get_image_cache_dir(self) -> Path:
        """画像图片缓存目录。"""
        try:
            d = self.ctx.paths.data_dir / "portrayal_images"
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            d = Path("data/plugins/rc.portrayal/portrayal_images")
            d.mkdir(parents=True, exist_ok=True)
            return d

    def _get_image_path(self, person_id: str, stream_id: str, version: int = 0) -> Path:
        """获取某用户在某群的画像图片路径(含版本号)。"""
        safe_id = re.sub(r'[^a-zA-Z0-9_]', '_', person_id)
        safe_stream = re.sub(r'[^a-zA-Z0-9_]', '_', stream_id)
        return self._get_image_cache_dir() / f"{safe_id}_{safe_stream}_v{version}.png"

    def _save_image_cache(self, img_b64: str, person_id: str, stream_id: str, version: int = 0) -> None:
        """将 base64 图片保存到缓存文件(与版本绑定)。"""
        try:
            import base64
            path = self._get_image_path(person_id, stream_id, version)
            path.write_bytes(base64.b64decode(img_b64))
        except Exception as exc:
            self.ctx.logger.debug("保存画像图片缓存失败: %s", exc)

    def _delete_image_cache(self, person_id: str, stream_id: str, version: int = 0) -> None:
        """删除指定版本的图片缓存。"""
        try:
            path = self._get_image_path(person_id, stream_id, version)
            if path.exists():
                path.unlink()
        except Exception as exc:
            self.ctx.logger.debug("删除画像图片缓存失败: %s", exc)

    @property
    def db(self) -> ProfileDB:
        if not hasattr(self, "_db") or self._db is None:
            self._db = ProfileDB(self._get_db_path())
        return self._db

    @property
    def collector(self) -> NapCatCollector:
        if not hasattr(self, "_collector") or self._collector is None:
            self._collector = NapCatCollector(
                per_query_count=self.config.portrayal.per_query_count,
                max_rounds=self.config.portrayal.max_rounds,
                max_msg_count=self.config.portrayal.message_limit,
                scan_hours=self.config.portrayal.scan_hours,
                cache_ttl_seconds=self.config.portrayal.cache_ttl_minutes * 60,
            )
        return self._collector

    # ─── 命令 ────────────────────────────────────────────────────────

    @Command("画像", pattern=r"^(?P<prefix>.)(?P<cmd>画像)(?:\s+(?P<target>.+))?$")
    async def handle_portrait(self, **kwargs):
        matched = kwargs.get("matched_groups", {}) or {}
        if matched.get("prefix", "") != self.config.portrayal.command_prefix:
            return False, "", 0
        return await self._execute_portrait(kwargs, mode="portrait")

    @Command("阴阳画像", pattern=r"^(?P<prefix>.)(?P<cmd>阴阳画像)(?:\s+(?P<target>.+))?$")
    async def handle_yin_yang_portrait(self, **kwargs):
        matched = kwargs.get("matched_groups", {}) or {}
        if matched.get("prefix", "") != self.config.portrayal.command_prefix:
            return False, "", 0
        return await self._execute_portrait(kwargs, mode="yin_yang")

    @Command("查看画像", pattern=r"^(?P<prefix>.)(?P<cmd>查看画像)(?:\s+(?P<target>.+))?$")
    async def handle_view_portrait(self, **kwargs):
        """查看已生成的画像"""
        matched = kwargs.get("matched_groups", {}) or {}
        if matched.get("prefix", "") != self.config.portrayal.command_prefix:
            return False, "", 0
        return await self._execute_view(kwargs)

    @Command("群分析", pattern=r"^(?P<prefix>.)(?P<cmd>群分析)$")
    async def handle_group_analysis(self, **kwargs):
        """分析当前群的话题、标签和用户关系"""
        matched = kwargs.get("matched_groups", {}) or {}
        if matched.get("prefix", "") != self.config.portrayal.command_prefix:
            return False, "", 0
        return await self._execute_group_analysis(kwargs)

    # ─── Hook 注入 ──────────────────────────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_request",
        name="portrayal_inject",
        description="插件画像优先注入 Replyer,无插件画像时保持 P004 原生兜底",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_replyer_before_request(self, **kwargs):
        if not self.config.portrayal.enable_injection:
            return {"action": "continue"}

        try:
            reply_tool_args = kwargs.get("reply_tool_args", {})
            if not isinstance(reply_tool_args, dict):
                return {"action": "continue"}

            session_id = str(kwargs.get("session_id", "") or "")
            if not session_id:
                return {"action": "continue"}

            reply_message_id = str(kwargs.get("reply_message_id", "") or "")
            if not reply_message_id:
                return {"action": "continue"}

            target_user_id = await self._get_sender_user_id(reply_message_id, session_id)
            if not target_user_id:
                return {"action": "continue"}

            platform = await self._detect_platform(session_id)
            person_id = await self._safe_get_person_id(platform, target_user_id)
            if not person_id:
                return {"action": "continue"}

            record = self.db.get_active_profile(person_id, stream_id=session_id)
            is_cross_group = False
            if not record:
                # 跨群聚合:仅在 enable_cross_group_merge 开启时查找其他群的画像
                if self.config.portrayal.enable_cross_group_merge:
                    all_records = self.db.get_all_active_for_person(person_id)
                    if len(all_records) == 1:
                        # 只有一个群的画像,直接用
                        record = all_records[0]
                        is_cross_group = True
                    elif len(all_records) >= 2:
                        # 多群画像:优先用最新的,但拼接所有群的来源标注
                        record = all_records[0]  # 最新版本
                        is_cross_group = True
                        # 如果有多群画像,拼接来源信息
                        if self.config.portrayal.enable_cross_group_merge:
                            merged = self._merge_cross_group_profiles(all_records)
                            if merged:
                                reply_tool_args["person_profile"] = merged
                                kwargs["reply_tool_args"] = reply_tool_args
                                self.ctx.logger.debug(
                                    "跨群画像已聚合注入: person_id=%s groups=%d",
                                    person_id, len(all_records),
                                )
                                return {"action": "continue", "modified_kwargs": kwargs}

            if not record:
                return {"action": "continue"}

            ttl = self.config.portrayal.profile_ttl_days
            if ttl > 0 and self.db.is_profile_expired(record, ttl):
                self.ctx.logger.debug("画像已过期,跳过注入: person_id=%s", person_id)
                return {"action": "continue"}

            profile_text = record.profile_text.strip()
            if profile_text:
                wrapped = self._format_hook_profile_block(profile_text, is_cross_group, record.source_group)
                reply_tool_args["person_profile"] = wrapped
                kwargs["reply_tool_args"] = reply_tool_args
                self.ctx.logger.debug(
                    "插件画像已注入: person_id=%s stream=%s cross_group=%s",
                    person_id, session_id, is_cross_group,
                )

            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:
            self.ctx.logger.error("hook 注入画像失败: %s", exc, exc_info=True)
            return {"action": "continue"}

    # ─── Planner Hook 注入 ────────────────────────────────────────

    @HookHandler(
        "maisaka.planner.before_request",
        name="portrayal_planner_inject",
        description="插件画像替换 Planner 中的原生画像,无插件画像时保持原生",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_before_request(self, **kwargs):
        if not self.config.portrayal.enable_planner_injection:
            return {"action": "continue"}

        try:
            messages = kwargs.get("messages", [])
            if not isinstance(messages, list) or not messages:
                self.ctx.logger.debug("Planner Hook: messages 为空,跳过")
                return {"action": "continue"}

            session_id = str(kwargs.get("session_id", "") or "")
            if not session_id:
                self.ctx.logger.debug("Planner Hook: session_id 为空,跳过")
                return {"action": "continue"}

            # 从 session 最近消息获取发送者
            target_user_id = await self._get_latest_sender_user_id(session_id)
            if not target_user_id:
                self.ctx.logger.info("Planner Hook: 未找到发送者, session=%s", session_id)
                return {"action": "continue"}

            platform = await self._detect_platform(session_id)
            person_id = await self._safe_get_person_id(platform, target_user_id)
            if not person_id:
                self.ctx.logger.info("Planner Hook: 未找到 person_id, user=%s", target_user_id)
                return {"action": "continue"}

            record = self.db.get_active_profile(person_id, stream_id=session_id)
            is_cross_group = False
            if not record and self.config.portrayal.enable_cross_group_merge:
                all_records = self.db.get_all_active_for_person(person_id)
                if all_records:
                    record = all_records[0]
                    is_cross_group = True

            if not record:
                self.ctx.logger.info("Planner Hook: 无画像记录, person=%s stream=%s cross_merge=%s", person_id, session_id, self.config.portrayal.enable_cross_group_merge)
                return {"action": "continue"}

            ttl = self.config.portrayal.profile_ttl_days
            if ttl > 0 and self.db.is_profile_expired(record, ttl):
                self.ctx.logger.info("Planner Hook: 画像已过期, person=%s", person_id)
                return {"action": "continue"}

            profile_text = record.profile_text.strip()
            if not profile_text:
                self.ctx.logger.info("Planner Hook: 画像文本为空, person=%s", person_id)
                return {"action": "continue"}

            # 构造 Planner 格式画像(模仿原生格式)
            person_name = record.person_name or target_user_id
            planner_profile = (
                "【人物画像-内部参考】\n"
                "以下内容仅供内部推理,不要向用户逐字复述。\n\n"
                f"- {person_name}(person_id: {person_id},来源: plugin_portrayal)\n"
                f"  {profile_text}\n\n"
                "使用时把它当作对当前人物的背景理解;若与当前对话冲突,以当前对话为准。"
            )

            # 查找并替换原生画像消息
            found = False
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.startswith("【人物画像-内部参考】"):
                        msg["content"] = planner_profile
                        found = True
                        break

            # 原生没注入画像 → 追加
            if not found:
                messages.append({"role": "user", "content": planner_profile})

            kwargs["messages"] = messages
            self.ctx.logger.info(
                "Planner 画像已注入: person=%s stream=%s replace=%s cross_group=%s",
                person_id, session_id, found, is_cross_group,
            )
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:
            self.ctx.logger.error("Planner hook 注入画像失败: %s", exc, exc_info=True)
            return {"action": "continue"}

    async def _get_latest_sender_user_id(self, session_id: str) -> str:
        """从 session 最近消息获取最新发送者 user_id。

        get_recent 返回时间正序（旧->新），取最后一条非空 user_id。
        正常对话流中最后一条是用户消息（用户发消息触发 Planner），
        bot 回复在用户消息之前，无需额外过滤。
        """
        try:
            result = await self.ctx.message.get_recent(chat_id=session_id, limit=10)
            if isinstance(result, dict):
                msgs = result.get("messages", [])
            elif isinstance(result, list):
                msgs = result
            else:
                return ""
            latest_uid = ""
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                msg_info = msg.get("message_info") or {}
                if not isinstance(msg_info, dict):
                    continue
                user_info = msg_info.get("user_info") or {}
                if isinstance(user_info, dict):
                    uid = str(user_info.get("user_id", "") or "").strip()
                    if uid:
                        latest_uid = uid
            return latest_uid
        except Exception as exc:
            self.ctx.logger.warning("获取最近发送者失败: %s", exc)
            return ""

    # ─── 用户消息统计(零 token)──────────────────────────────────

    def _compute_user_stats(self, target_msgs, target_user_id: str, all_msgs=None) -> str:
        """对目标用户的消息做分词统计,返回 JSON 字符串。

        产出:top_words、catchphrases、msg_count、radar_scores(规则维度)、relations(加权)
        LLM 维度(输出力/整活值/知识值)由 _compute_llm_scores 异步填充。
        """
        if not target_msgs:
            return ""
        # 尝试导入 jieba
        try:
            import jieba
            import jieba.posseg as pseg
            _use_jieba = True
        except ImportError:
            _use_jieba = False
            cn_pattern = _re.compile(r'[\u4e00-\u9fff]{2,4}')

        stop_words = {
            "因为","所以","但是","而且","不过","然后","可以","应该","这个","那个",
            "什么","怎么","这么","那么","如果","虽然","已经","还是","或者","就是",
            "没有","不是","不要","不能","知道","觉得","可能","只是","真的","好的",
            "的话","的人","了的","你们","我们","他们","自己","现在","今天","昨天",
            "明天","一样","一直","一下","时候","为什么","是不是","能不能","有没有",
            "哈哈哈哈","哈哈","嗯嗯","啊啊","好吧","确实","其实","感觉","觉得","的话",
            "一下","一种","一般","一起","一直","还是","或者","这种","那种","这些",
            "那些","这样","那样","怎么","什么","为什么","的话","的人","了的","不了",
            "不是","不会","不能","不要","没有","没什么","怎么办","怎么样","什么样",
            "是不是","能不能","有没有","差不多了","差不多","这样吧","之类的","什么的",
            "还是","还有","还有个","还是说","还是那","好像","反正","毕竟","其实吧",
            "的话","那么","这么","这种","那种","这种事","那种事","这种话","那种话",
            "一个","一些","一直","一定","一般","一起","一下","一样的","一样了",
            "不是吗","不是嘛","不是的","不行吗","不行嘛","不好吗","不好嘛","不对吗",
            "不对嘛","不会吧","不会的","不是吧","不是嘛","不是吗","不是的","不是了",
            "这种","那种","这些","那些","这样","那样","这么","那么","这么点",
            "那种","那啥","那货","那个","这货","这个","这边","那边","那里","这里",
            "怎么","什么","为什么","啥的","啥事","啥啊","啥子","啥玩意","啥东西",
            "什么鬼","什么呀","啥意思","啥情况","啥问题","啥事啊","啥玩意儿",
            "的话","的人","了的","不了","也是","也好","也罢","也是的","也是吧",
            "还是","还有","还有个","还是说","还是那","好像","反正","毕竟","其实吧",
            "的话","那么","这么","这种","那种","这种事","那种事","这种话","那种话",
            "一个","一些","一直","一定","一般","一起","一下","一样的","一样了",
            "那种","那啥","那货","那个","这货","这个","这边","那边","那里","这里",
            # bot 系统消息词(过滤掉画像插件自身产生的词汇)
            "画像","正在","扫描","消息","生成","刷新","版本","保存","后续","回复",
            "参考","指令","发送","稍等","重新","提示","无法","获取","失败","成功",
            "采集","分析","天前","天内","如需","强制","文本","发言","群消","已扫描",
            "提取","条群","活跃","锁定","解锁","停用","删除","恢复","配置","设置",
            "端口","监听","地址","密钥","模型","温度","重试","截断","字符","提醒",
            "天数","过期","注入","渲染","图片","展示","注入版","展示版","命令","前缀",
            "权限","白名单","保护","名单","群组","群名","群号","群名片","昵称",
            "常规","阴阳","对比","历史","版本号","性格","标签","话题","词云","词性",
            "过滤","停用词","高频","口头","表达","风格","丰富","维度","雷达","图谱",
            "表达风格","相处建议","稳定偏好","性格标签","优势特质","潜在短板",
            "语言风格","行为模式","社交角色","互动倾向","话题偏好","边界","雷区",
        }
        word_counter = _Counter()
        phrase_counter = _Counter()
        word_pos = {}  # word -> POS tag
        # 画像插件输出特征词:如果消息包含这些词,说明是 bot 发的画像文本而非用户聊天
        _bot_text_markers = (
            '正在扫描', '已扫描', '条群消息', '提取到', '条「', '的文本发言',
            '画像已保存', '后续回复将参考', '天内免刷', '天前已生成',
            '如需强制', '正在生成', '正在对比', '正在分析',
            '版本已保存', '请稍等', '正在采集',
            # LLM 画像输出结构化标签
            '【性格标签】', '【优势特质】', '【潜在短板】', '【相处建议】',
            '【语言风格】', '【行为模式】', '【社交角色】',
            '稳定偏好', '表达风格', '性格标签', '优势特质',
            '潜在短板', '相处建议', '语言风格', '行为模式',
            '社交角色', '互动倾向', '话题偏好',
        )
        for m in target_msgs:
            text = m.text if hasattr(m, 'text') else str(m)
            # 跳过 bot 系统消息和画像输出文本
            if any(p in text for p in _bot_text_markers):
                continue
            # 去除 URL、@提及、表情代码等
            text = _re.sub(r'https?://\S+', '', text)
            text = _re.sub(r'@\d+', '', text)
            text = _re.sub(r'\[.+?\]', '', text)

            if _use_jieba:
                # jieba 词性标注分词
                words_with_pos = pseg.lcut(text)
                for w in words_with_pos:
                    word = w.word.strip()
                    flag = w.flag
                    # 过滤:长度>=2、包含中文、不是停用词、不是纯标点/数字/英文短词
                    if len(word) >= 2 and word not in stop_words and _re.search(r'[\u4e00-\u9fff]', word) and not _re.match(r'^[\W\d]+$', word):
                        word_counter[word] += 1
                        # 同时记录词性
                        if word not in word_pos:
                            word_pos[word] = flag
            else:
                # 回退:正则匹配
                for match in cn_pattern.finditer(text):
                    w = match.group()
                    if w not in stop_words:
                        word_counter[w] += 1

            # 口头禅(ChatLab 式:整条消息频率统计,不分词不 n-gram)
            _media_placeholders = {
                '[图片]', '[视频]', '[语音]', '[文件]', '[动画表情]', '[表情]',
                '[链接]', '[位置]', '[地理位置]', '[名片]', '[红包]', '[转账]',
                '[音乐]', '[回复消息]', '[Image]', '[Photo]', '[Video]',
                '[Voice]', '[File]', '[Sticker]', '[Link]', '[Location]',
            }
            # 用原始消息文本(只去 URL 和 @提及,不去方括号)
            raw_text = m.text if hasattr(m, 'text') else str(m)
            raw_text = _re.sub(r'https?://\S+', '', raw_text)
            raw_text = _re.sub(r'@\d+', '', raw_text)
            raw_text = raw_text.strip()
            if len(raw_text) >= 2 and raw_text not in _media_placeholders:
                if not _re.match(r'^[+/!!##]', raw_text):
                    phrase_counter[raw_text] += 1

        top_words = [{"word": w, "count": c, "pos": word_pos.get(w, "")} for w, c in word_counter.most_common(100)]
        catchphrases = [{"phrase": p, "count": c} for p, c in phrase_counter.most_common(40) if c >= 2]
        # ── 雷达图规则统计(零 token)──
        msg_count = len(target_msgs)
        # 在线天数估算:首末消息时间跨度
        timestamps = [m.timestamp for m in target_msgs if hasattr(m, 'timestamp') and m.timestamp > 0]
        if timestamps:
            span_seconds = max(timestamps) - min(timestamps)
            online_days = max(1, span_seconds / 86400.0)
        else:
            online_days = 1.0
        daily_avg = msg_count / online_days
        # 水群力:日均发言×8+20,min 100
        shuiqun = min(100, round(daily_avg * 8 + 20))
        # 被@次数(从全群消息文本中统计)
        at_count = 0
        if all_msgs:
            at_pattern = _re.compile(r'@' + _re.escape(str(target_user_id)))
            for m in all_msgs:
                if at_pattern.search(m.text if hasattr(m, 'text') else str(m)):
                    at_count += 1
        # 群影响力:被@×5 + 发言后被回复数×3,min 100
        # 发言后被回复数:统计 target 用户消息后紧接的其他用户消息数
        reply_after_count = 0
        if all_msgs:
            for i, m in enumerate(all_msgs):
                if hasattr(m, 'user_id') and m.user_id == target_user_id:
                    if i + 1 < len(all_msgs) and all_msgs[i + 1].user_id != target_user_id:
                        reply_after_count += 1
        influence = min(100, at_count * 5 + reply_after_count * 3)
        # 社交力:互动不同用户数×10,min 100
        interacted_users = set()
        if all_msgs:
            for i, m in enumerate(all_msgs):
                if hasattr(m, 'user_id') and m.user_id == target_user_id:
                    if i > 0 and all_msgs[i - 1].user_id != target_user_id:
                        interacted_users.add(all_msgs[i - 1].user_id)
                    if i + 1 < len(all_msgs) and all_msgs[i + 1].user_id != target_user_id:
                        interacted_users.add(all_msgs[i + 1].user_id)
        social = min(100, len(interacted_users) * 10)
        radar_scores = {
            "水群力": shuiqun,
            "输出力": 0,  # LLM 填充
            "整活值": 0,  # LLM 填充
            "社交力": social,
            "知识值": 0,  # LLM 填充
            "群影响力": influence,
        }
        # ── 关系图谱(加权统计)──
        relations = {}
        if all_msgs:
            for i, m in enumerate(all_msgs):
                if not hasattr(m, 'user_id') or m.user_id != target_user_id:
                    continue
                # 前一条消息的用户
                if i > 0:
                    prev = all_msgs[i - 1]
                    if prev.user_id != target_user_id:
                        gap = abs(m.timestamp - prev.timestamp) if hasattr(m, 'timestamp') and hasattr(prev, 'timestamp') else 999
                        weight = 3 if gap < 30 else (1 if gap > 300 else 2)
                        relations[prev.user_id] = relations.get(prev.user_id, 0) + weight
                # 后一条消息的用户
                if i + 1 < len(all_msgs):
                    nxt = all_msgs[i + 1]
                    if nxt.user_id != target_user_id:
                        gap = abs(nxt.timestamp - m.timestamp) if hasattr(nxt, 'timestamp') and hasattr(m, 'timestamp') else 999
                        weight = 3 if gap < 30 else (1 if gap > 300 else 2)
                        relations[nxt.user_id] = relations.get(nxt.user_id, 0) + weight
                # @关系 ×5
                at_matches = _re.findall(r'@(\d{5,12})', m.text if hasattr(m, 'text') else str(m))
                for at_uid in at_matches:
                    if at_uid != target_user_id:
                        relations[at_uid] = relations.get(at_uid, 0) + 5
        # 限制 relations 到 Top 20
        sorted_rel = sorted(relations.items(), key=lambda x: -x[1])[:20]
        relations_out = {uid: cnt for uid, cnt in sorted_rel}
        stats = {
            "top_words": top_words,
            "catchphrases": catchphrases,
            "msg_count": msg_count,
            "pos_tags": {
                "n": "名词", "nr": "人名", "ns": "地名", "nt": "机构名", "nz": "其他专名",
                "nw": "作品名", "v": "动词", "vn": "动名词", "a": "形容词", "ad": "副形词",
                "an": "名形词", "i": "成语", "l": "习用语", "g": "语素", "j": "简称",
                "t": "时间词", "s": "处所词", "f": "方位词", "m": "数词", "q": "量词",
                "r": "代词", "p": "介词", "c": "连词", "u": "助词", "x": "非语素字",
                "d": "副词", "h": "前缀", "k": "后缀", "e": "叹词", "o": "拟声词",
            },
            "meaningful_tags": ["n","nr","ns","nt","nz","nw","a","i","l","j"],
            "radar_scores": radar_scores,
            "relations": relations_out,
            "daily_avg": round(daily_avg, 1),
            "online_days": round(online_days, 1),
        }
        return _json.dumps(stats, ensure_ascii=False)

    # ─── LLM 六维打分(并行调用)──────────────────────────────

    async def _compute_llm_scores(self, target_msgs, target_name: str) -> dict:
        """并行调用 LLM 对输出力/整活值/知识值打分,返回 dict。"""
        if not target_msgs:
            return {"output_power": 0, "meme_power": 0, "knowledge_power": 0}
        # 拼接目标用户消息(最多 2000 字符)
        msg_text = "\n".join(
            (m.text if hasattr(m, 'text') else str(m))[:200]
            for m in target_msgs[:50]
        )[:2000]
        prompt = (
            "你是一个群聊用户行为打分助手。根据以下发言记录,对三个维度打分(0-100)。\n"
            "输出力:反驳/争论/攻击性语言/吐槽的频率和强度\n"
            "整活值:梗图/表情包/抽象发言/二创的参与度\n"
            "知识值:解答问题/教程/技术讨论的深度\n"
            "\n发言记录(目标用户:「" + target_name + "」):\n" + msg_text +
            "\n\n仅输出 JSON:{\"output_power\": N, \"meme_power\": N, \"knowledge_power\": N}"
        )
        try:
            resp = await self._call_llm_with_retry("你是打分助手,只输出JSON。", prompt)
            if resp:
                import json as _j2
                # 提取 JSON
                m = _re.search(r'\{[^}]+\}', resp)
                if m:
                    scores = _j2.loads(m.group())
                    return {
                        "output_power": max(0, min(100, int(scores.get("output_power", 0)))),
                        "meme_power": max(0, min(100, int(scores.get("meme_power", 0)))),
                        "knowledge_power": max(0, min(100, int(scores.get("knowledge_power", 0)))),
                    }
        except Exception as exc:
            self.ctx.logger.warning("LLM 打分失败: %s", exc)
        return {"output_power": 0, "meme_power": 0, "knowledge_power": 0}

    # ─── 画像分析核心流程 ────────────────────────────────────────────

    async def _execute_portrait(self, kwargs: dict, mode: str) -> tuple:
        # 热加载检查:每次执行指令时检测 prompt 文件变更
        self._check_prompts_hot_reload()
        stream_id = kwargs.get("stream_id", "")
        platform = kwargs.get("platform", "")
        sender_user_id = kwargs.get("user_id", "")
        matched = kwargs.get("matched_groups", {}) or {}
        msg_obj = kwargs.get("message", {}) or {}
        raw_segments: list[dict] = msg_obj.get("raw_message", []) if isinstance(msg_obj, dict) else []

        # 从原始消息段提取 @ 的 QQ 号(astrbot_plugin_portrayal 同款做法)
        at_qqs = self._extract_at_qqs(raw_segments)
        self.ctx.logger.debug("原始消息 @ QQ: %s", at_qqs)

        if not stream_id:
            await self.ctx.send.text("无法获取聊天流信息", stream_id)
            return False, "无聊天流", 1

        # 权限检查
        if not self._check_permission(sender_user_id):
            await self.ctx.send.text("你没有权限使用此命令", stream_id)
            return False, "无权限", 1

        target_text = matched.get("target", "").strip()
        # 提取标志
        force_refresh = "--force" in target_text
        diff_mode = "--diff" in target_text
        target_text = target_text.replace("--force", "").replace("--diff", "").strip()

        # 1. 解析目标用户(优先用原始 @ 中的 QQ 号)
        target_user_id, target_person_id, target_name = await self._resolve_target(
            platform=platform, sender_user_id=sender_user_id, target_text=target_text,
            at_qqs=at_qqs,
        )
        self.ctx.logger.info("目标解析: name=%s person_id=%s user_id=%s", target_name, target_person_id, target_user_id)

        # 用群名片覆盖目标名(每个群可能不同)
        if target_user_id:
            gid = await self._get_group_id(stream_id)
            if gid:
                card, nick = await self._fetch_group_card_and_nick(gid, target_user_id)
                if card and nick and card != nick:
                    target_name = f"{card}({nick})"
                elif card:
                    target_name = card
                elif nick:
                    target_name = nick

        if not target_person_id:
            hint = "请先让该用户与 bot 互动几次以注册身份" if target_user_id else "未找到该用户"
            await self.ctx.send.text(f"无法解析目标用户:{hint}", stream_id)
            return False, hint, 1

        # 2. 保护名单检查
        if target_user_id and self._is_protected(target_user_id):
            await self.ctx.send.text("该用户在保护名单中,不允许分析", stream_id)
            return False, "受保护用户", 1

        await self.ctx.send.text(f"正在扫描最近 {self.config.portrayal.scan_hours // 24} 天的群消息...", stream_id)

        # 3. 检查已有画像(TTL 内仅提示,不阻断)
        existing = self.db.get_active_profile(target_person_id, stream_id=stream_id)
        if existing:
            days = self._days_since(existing.updated_at)
            remind = self.config.portrayal.refresh_remind_days
            if days < remind:
                await self.ctx.send.text(
                    f"「{target_name}」的画像在 {days} 天前已生成,正在重新采集...",
                    stream_id,
                )

        existing_profile_text = existing.profile_text if existing else "(无)"

        # --diff: 对比当前版本与上一版本
        if diff_mode:
            return await self._execute_diff(
                stream_id=stream_id, target_name=target_name,
                target_person_id=target_person_id, existing=existing,
            )

        # 4. 从 stream_id 反查 group_id
        group_id = await self._get_group_id(stream_id)
        if not group_id:
            await self.ctx.send.text("无法获取群号,跳过消息采集", stream_id)
            return False, "无群号", 1

        # 顺手刷新群名缓存(失败不阻断主流程)
        try:
            await self._resolve_group_name(group_id)
        except Exception:
            pass

        # 5. NapCat API 采集消息
        try:
            result = await self.collector.collect(
                api_call_fn=self.ctx.api.call,
                group_id=group_id,
                target_user_id=target_user_id,
            )
        except Exception as exc:
            self.ctx.logger.error("消息采集失败: %s", exc, exc_info=True)
            await self.ctx.send.text("消息采集失败,请稍后重试", stream_id)
            return False, "采集失败", 1

        if result.is_empty:
            self.ctx.logger.info("采集结果: total_scanned=%d target_count=%d error=%s",
                                 result.total_scanned, result.target_count, result.error)
            if result.error:
                self.ctx.logger.warning("采集失败详情: %s", result.error)
                await self.ctx.send.text(f"消息采集失败:{result.error}", stream_id)
            else:
                self.ctx.logger.info("采集空结果: total_scanned=%d target_count=%d last_debug=%s",
                                    result.total_scanned, result.target_count,
                                    getattr(result, '_last_debug', 'N/A'))
                await self.ctx.send.text(
                    f"在最近 {self.config.portrayal.scan_hours // 24} 天内未找到「{target_name}」的文本发言",
                    stream_id,
                )
            return False, "无目标消息", 1

        # 6. 目标发言数硬拦截
        min_msgs = self.config.portrayal.min_target_messages
        self.ctx.logger.info(
            "采集完成: total_scanned=%d target_count=%d api_calls=%d debug=%s",
            result.total_scanned, result.target_count, result.api_calls,
            result.error or "ok",
        )
        if result.target_count < min_msgs:
            await self.ctx.send.text(
                f"目标用户「{target_name}」仅有 {result.target_count} 条文本发言(需至少 {min_msgs} 条),样本不足",
                stream_id,
            )
            return False, "样本不足", 1

        # 7. 格式化消息(目标消息主导 + 少量上下文)
        max_chars = self.config.portrayal.chat_log_max_chars
        target_msgs = list(result.target_messages)
        context_msgs = [m for m in result.all_messages if m.user_id != target_user_id]
        # 上下文控制在目标消息的 2 倍以内
        max_context = min(len(target_msgs) * 2, len(context_msgs))
        selected = target_msgs + context_msgs[:max_context]
        selected.sort(key=lambda m: m.timestamp)
        chat_log = self._format_messages_with_marker(selected, target_user_id, target_name)
        chat_log = chat_log[:max_chars]

        # 8. LLM 分析
        today = datetime.now().strftime("%Y-%m-%d")
        total_scanned = result.total_scanned

        mode_label = "阴阳画像" if mode == "yin_yang" else "画像"
        await self.ctx.send.text(
            f"已扫描 {total_scanned} 条群消息,提取到 {result.target_count} 条「{target_name}」的文本发言,正在生成{mode_label}...",
            stream_id,
        )

        if mode == "yin_yang":
            system_prompt = self._prompts.get("yin_yang_system", _DEFAULT_YIN_YANG_SYSTEM)
            user_prompt = self._safe_format_prompt(
                self._prompts.get("yin_yang_user", _DEFAULT_YIN_YANG_USER),
                target_name=target_name, chat_log=chat_log,
                existing_profile=existing_profile_text,
                date=today, msg_count=total_scanned, group_id=group_id,
            )
        else:
            system_prompt = self._prompts.get("portrait_system", _DEFAULT_PORTRAIT_SYSTEM)
            user_prompt = self._safe_format_prompt(
                self._prompts.get("portrait_user", _DEFAULT_PORTRAIT_USER),
                target_name=target_name, chat_log=chat_log,
                existing_profile=existing_profile_text,
                date=today, msg_count=total_scanned, group_id=group_id,
            )

        _gen_t0 = time.time()
        # 并行:画像生成 + 六维打分
        import asyncio as _aio
        profile_task = self._call_llm_with_retry(system_prompt, user_prompt)
        score_task = self._compute_llm_scores(target_msgs, target_name)
        results = await _aio.gather(profile_task, score_task, return_exceptions=True)
        response_text = results[0] if not isinstance(results[0], Exception) else ""
        llm_scores = results[1] if not isinstance(results[1], Exception) else {"output_power": 0, "meme_power": 0, "knowledge_power": 0}
        _usage = dict(self._last_usage or {})
        _duration_ms = int((time.time() - _gen_t0) * 1000)
        if not response_text:
            await self.ctx.send.text("画像生成失败,LLM 调用出错", stream_id)
            self._safe_add_gen_log(
                operator_user_id=sender_user_id, target_user_id=target_user_id,
                target_person_id=target_person_id, target_name=target_name,
                stream_id=stream_id, source_group=group_id, mode=mode,
                scanned_msgs=total_scanned, target_msgs=result.target_count,
                usage=_usage, duration_ms=_duration_ms, success=False,
                error="LLM 调用出错", record_id=0, version=0,
            )
            return False, "LLM 失败", 1

        # 9. 解析 LLM 输出
        display_text, override_text = self._parse_llm_output(response_text, today, total_scanned, group_id)

        # 10. 保存到独立 SQLite
        # 10a. 计算用户消息统计(零 token)
        user_stats = self._compute_user_stats(target_msgs, target_user_id, all_msgs=list(result.all_messages))
        # 10b. 合并 LLM 打分到 user_stats
        if user_stats:
            try:
                import json as _j3
                stats_dict = _j3.loads(user_stats)
                if "radar_scores" in stats_dict:
                    stats_dict["radar_scores"]["输出力"] = llm_scores.get("output_power", 0)
                    stats_dict["radar_scores"]["整活值"] = llm_scores.get("meme_power", 0)
                    stats_dict["radar_scores"]["知识值"] = llm_scores.get("knowledge_power", 0)
                user_stats = _j3.dumps(stats_dict, ensure_ascii=False)
            except Exception:
                pass
        try:
            record = self.db.save_profile(
                person_id=target_person_id, person_name=target_name,
                stream_id=stream_id, source_group=group_id,
                profile_text=override_text, display_text=display_text,
                mode=mode, msg_count=result.total_scanned,
                target_msg_count=result.target_count,
                user_id=target_user_id,
                user_stats=user_stats,
            )
            self.ctx.logger.info("画像已保存: person_id=%s version=%d", target_person_id, record.version)
        except Exception as exc:
            self.ctx.logger.error("保存画像失败: %s", exc, exc_info=True)
            await self.ctx.send.text(f"画像已生成但保存失败:{exc}", stream_id)
            self._safe_add_gen_log(
                operator_user_id=sender_user_id, target_user_id=target_user_id,
                target_person_id=target_person_id, target_name=target_name,
                stream_id=stream_id, source_group=group_id, mode=mode,
                scanned_msgs=total_scanned, target_msgs=result.target_count,
                usage=_usage, duration_ms=_duration_ms, success=False,
                error=f"保存失败: {exc}", record_id=0, version=0,
            )
            return False, "保存失败", 1

        # 记录生成日志(含真实 token 用量)
        self._safe_add_gen_log(
            operator_user_id=sender_user_id, target_user_id=target_user_id,
            target_person_id=target_person_id, target_name=target_name,
            stream_id=stream_id, source_group=group_id, mode=mode,
            scanned_msgs=total_scanned, target_msgs=result.target_count,
            usage=_usage, duration_ms=_duration_ms, success=True,
            error="", record_id=record.id, version=record.version,
        )

        # 11. 发送展示版
        status = f"\n\n✅ 画像已保存(版本 {record.version}),后续回复将参考此画像"
        if len(display_text) > 3000:
            display_text = display_text[:3000] + "\n...(已截断)"

        # 尝试图片渲染
        if self.config.portrayal.enable_image_output:
            try:
                img_b64 = await self._render_portrait_card(
                    display_text, target_name, today, total_scanned, group_id, mode,
                )
                if img_b64:
                    self._save_image_cache(img_b64, target_person_id, stream_id, record.version)
                    await self.ctx.send.image(img_b64, stream_id)
                    await self.ctx.send.text(status.strip(), stream_id)
                    return True, f"画像完成: {target_name}", 2
            except Exception as exc:
                self.ctx.logger.warning("图片渲染失败,回退文本: %s", exc)

        await self.ctx.send.text(display_text + status, stream_id)
        return True, f"画像完成: {target_name}", 2

    # ─── 版本对比 ────────────────────────────────────────────────────

    async def _execute_diff(self, *, stream_id: str, target_name: str,
                           target_person_id: str, existing: Optional[ProfileRecord]) -> tuple:
        """对比当前版本与上一版本的画像变化。"""
        if not existing:
            await self.ctx.send.text(f"「{target_name}」暂无画像记录,无法对比", stream_id)
            return False, "无画像", 1

        history = self.db.get_history(target_person_id, stream_id)
        if len(history) < 2:
            await self.ctx.send.text(f"「{target_name}」仅有 {len(history)} 个版本,需要至少 2 个版本才能对比", stream_id)
            return False, "版本不足", 1

        current = history[0]   # 最新版本
        previous = history[1]  # 上一版本

        await self.ctx.send.text(
            f"正在对比「{target_name}」的画像变化(v{previous.version} → v{current.version})...",
            stream_id,
        )

        system_prompt = (
            "你是一个人物画像对比分析师。"
            "请对比用户的新旧两版画像,指出关键变化。\n\n"
            "输出格式要求:\n"
            "===展示版===\n"
            "用要点列出变化,每条前加标记:\n"
            "🟢 新增的特征(旧版没有的)\n"
            "🔴 消失的特征(新版没有的)\n"
            "🟡 变化的特征(描述从什么变成了什么)\n"
            "⚪ 稳定的特征(两版一致的核心特征,简列即可)\n"
            "语气客观,不需要重复完整画像内容。\n"
            "===注入版===\n"
            "(对比摘要,用于注入到回复系统作为参考)"
        )
        user_prompt = (
            f"目标用户:{target_name}\n\n"
            f"--- 旧版画像(v{previous.version},{self._days_since(previous.updated_at)}天前)---\n"
            f"{previous.display_text}\n\n"
            f"--- 新版画像(v{current.version},{self._days_since(current.updated_at)}天前)---\n"
            f"{current.display_text}\n\n"
            f"请对比这两版画像的变化。"
        )

        try:
            raw_output = await self._call_llm_with_retry(system_prompt, user_prompt)
            _usage = dict(self._last_usage or {})
            display_text, override_text = self._parse_llm_output(
                raw_output, datetime.now().strftime('%Y-%m-%d'), 0, "",
            )
        except Exception as exc:
            self.ctx.logger.error("对比 LLM 调用失败: %s", exc, exc_info=True)
            await self.ctx.send.text("对比分析失败,LLM 调用出错", stream_id)
            return False, "LLM 失败", 1

        # 发送结果
        if self.config.portrayal.enable_image_output:
            try:
                img_b64 = await self._render_portrait_card(
                    display_text, target_name,
                    datetime.now().strftime('%Y-%m-%d'), 0, "", "portrait",
                )
                if img_b64:
                    await self.ctx.send.image(img_b64, stream_id)
                    await self.ctx.send.text(
                        f"✅ 对比完成(v{previous.version} → v{current.version})",
                        stream_id,
                    )
                    return True, f"对比完成: {target_name}", 2
            except Exception as exc:
                self.ctx.logger.warning("对比图片渲染失败,回退文本: %s", exc)

        await self.ctx.send.text(
            display_text + f"\n\n✅ 对比完成(v{previous.version} → v{current.version})",
            stream_id,
        )
        return True, f"对比完成: {target_name}", 2

    # ─── WebUI 采集回调 ──────────────────────────────────────────────

    async def _webui_collect(self, *, person_id: str, user_id: str, mode: str, force: bool = False, group_id: str = "") -> dict:
        """WebUI 触发的画像采集回调。

        如果传了 group_id,用 group_id 直接采集(新用户路径)。
        否则从已有画像记录里拿 stream_id → group_id(已有用户路径)。
        返回 dict: {success, error, record_id, version}
        """
        try:
            # 如果传了 person_id 为空但传了 user_id,自动获取 person_id
            if not person_id and user_id:
                person_id = await self._safe_get_person_id("qq", user_id)
            if not person_id:
                return {"success": False, "error": "缺少 person_id"}

            stream_id = ""
            existing = self.db.get_active_profile(person_id, stream_id="")

            if group_id:
                # 直接传入的 group_id,反查 stream_id
                streams = await self.ctx.chat.get_group_streams()
                if isinstance(streams, list):
                    for s in streams:
                        if isinstance(s, dict) and str(s.get("group_id", "")) == str(group_id):
                            stream_id = str(s.get("stream_id", s.get("session_id", "")))
                            break
                if not stream_id:
                    stream_id = str(group_id)
                # 按 stream_id 重新查该群的已有画像(而不是跨群的最新画像)
                existing = self.db.get_active_profile(person_id, stream_id=stream_id) or existing
            else:
                # 已有用户路径:从画像记录拿 stream_id
                stream_id = existing.stream_id if existing else ""
                if not stream_id:
                    return {"success": False, "error": "未找到该用户的历史记录,无法确定群聊"}
                # 从 stream_id 反查 group_id
                group_id = await self._get_group_id(stream_id)
                if not group_id:
                    return {"success": False, "error": "无法获取群号"}

            # 获取用户名:优先取群名片+QQ昵称
            target_name = user_id or person_id
            # 尝试从群成员信息获取群名片和昵称
            if group_id and user_id:
                card, nick = await self._fetch_group_card_and_nick(group_id, user_id)
                self.ctx.logger.info("群名片获取: group=%s user=%s card=%s nick=%s", group_id, user_id, card or '(空)', nick or '(空)')
                if card and nick and card != nick:
                    target_name = f"{card}({nick})"
                elif card:
                    target_name = card
                elif nick:
                    target_name = nick
            if target_name == user_id or target_name == person_id:
                # 没拿到群名片,用已有记录或 QQ 昵称
                if existing:
                    target_name = existing.person_name or target_name
                elif user_id:
                    pid_name = await self._safe_get_person_name(person_id)
                    if pid_name and not pid_name.startswith("未知"):
                        target_name = pid_name
                    else:
                        target_name = await self._fetch_qq_nickname(user_id) or user_id

            # 采集消息
            self.ctx.logger.info("WebUI 采集: group_id=%s user_id=%s stream_id=%s", group_id, user_id, stream_id)
            result = await self.collector.collect(
                api_call_fn=self.ctx.api.call,
                group_id=group_id,
                target_user_id=user_id,
            )
            self.ctx.logger.info("采集结果: is_empty=%s target_count=%d total=%d error=%s",
                                result.is_empty, result.target_count, result.total_scanned, result.error or '')

            if result.is_empty:
                return {"success": False, "error": result.error or "未找到该用户的发言"}

            min_msgs = self.config.portrayal.min_target_messages
            if result.target_count < min_msgs:
                return {"success": False, "error": f"仅有 {result.target_count} 条发言(需至少 {min_msgs} 条),样本不足"}

            # 格式化消息
            target_msgs = list(result.target_messages)
            context_msgs = [m for m in result.all_messages if m.user_id != user_id]
            max_context = min(len(target_msgs) * 2, len(context_msgs))
            selected = target_msgs + context_msgs[:max_context]
            selected.sort(key=lambda m: m.timestamp)
            chat_log = self._format_messages_with_marker(selected, user_id, target_name)
            chat_log = chat_log[:self.config.portrayal.chat_log_max_chars]

            # LLM 分析
            existing_profile_text = existing.profile_text if existing else "(无)"
            today = datetime.now().strftime('%Y-%m-%d')

            if mode == "yin_yang":
                system_prompt = self._prompts.get("yin_yang_system", _DEFAULT_YIN_YANG_SYSTEM)
                user_prompt = self._safe_format_prompt(
                    self._prompts.get("yin_yang_user", _DEFAULT_YIN_YANG_USER),
                    target_name=target_name, chat_log=chat_log,
                    existing_profile=existing_profile_text,
                    date=today, msg_count=result.total_scanned, group_id=group_id,
                )
            else:
                system_prompt = self._prompts.get("portrait_system", _DEFAULT_PORTRAIT_SYSTEM)
                user_prompt = self._safe_format_prompt(
                    self._prompts.get("portrait_user", _DEFAULT_PORTRAIT_USER),
                    target_name=target_name, chat_log=chat_log,
                    existing_profile=existing_profile_text,
                    date=today, msg_count=result.total_scanned, group_id=group_id,
                )

            raw_output = await self._call_llm_with_retry(system_prompt, user_prompt)
            _usage = dict(self._last_usage or {})
            display_text, override_text = self._parse_llm_output(
                raw_output, today, result.total_scanned, group_id,
            )

            if not override_text:
                return {"success": False, "error": "LLM 输出解析失败"}

            # 并行打分
            llm_scores = await self._compute_llm_scores(list(result.target_messages), target_name)

            # 保存
            user_stats = self._compute_user_stats(list(result.target_messages), user_id, all_msgs=list(result.all_messages))
            # 合并 LLM 打分
            if user_stats:
                try:
                    import json as _j4
                    stats_dict = _j4.loads(user_stats)
                    if "radar_scores" in stats_dict:
                        stats_dict["radar_scores"]["输出力"] = llm_scores.get("output_power", 0)
                        stats_dict["radar_scores"]["整活值"] = llm_scores.get("meme_power", 0)
                        stats_dict["radar_scores"]["知识值"] = llm_scores.get("knowledge_power", 0)
                    user_stats = _j4.dumps(stats_dict, ensure_ascii=False)
                except Exception:
                    pass
            record = self.db.save_profile(
                person_id=person_id, person_name=target_name,
                stream_id=stream_id, source_group=group_id,
                profile_text=override_text, display_text=display_text,
                mode=mode, msg_count=result.total_scanned,
                target_msg_count=result.target_count,
                user_id=user_id,
                user_stats=user_stats,
            )

            # 渲染并缓存图片
            if self.config.portrayal.enable_image_output:
                try:
                    img_b64 = await self._render_portrait_card(
                        display_text, target_name, today, result.total_scanned, group_id, mode,
                    )
                    if img_b64:
                        self._save_image_cache(img_b64, person_id, stream_id, record.version)
                except Exception as exc:
                    self.ctx.logger.debug("WebUI 采集: 图片渲染失败: %s", exc)

            self.ctx.logger.info("WebUI 采集完成: person_id=%s version=%d", person_id, record.version)
            self._safe_add_gen_log(
                operator_user_id="webui", target_user_id=user_id,
                target_person_id=person_id, target_name=target_name,
                stream_id=stream_id, source_group=group_id, mode=mode,
                scanned_msgs=result.total_scanned, target_msgs=result.target_count,
                usage=_usage, duration_ms=0, success=True,
                error="", record_id=record.id, version=record.version,
            )
            return {"success": True, "record_id": record.id, "version": record.version}

        except Exception as exc:
            self.ctx.logger.error("WebUI 采集失败: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    # ─── 查看画像 ────────────────────────────────────────────────────

    async def _execute_view(self, kwargs: dict) -> tuple:
        stream_id = kwargs.get("stream_id", "")
        platform = kwargs.get("platform", "")
        sender_user_id = kwargs.get("user_id", "")
        matched = kwargs.get("matched_groups", {}) or {}
        msg_obj = kwargs.get("message", {}) or {}
        raw_segments: list[dict] = msg_obj.get("raw_message", []) if isinstance(msg_obj, dict) else []
        at_qqs = self._extract_at_qqs(raw_segments)
        target_text = matched.get("target", "").strip()

        if not stream_id:
            await self.ctx.send.text("无法获取聊天流信息", stream_id)
            return False, "无聊天流", 1

        # 统一用 _resolve_target(与画像分析保持一致)
        target_user_id, person_id, name = await self._resolve_target(
            platform=platform, sender_user_id=sender_user_id,
            target_text=target_text, at_qqs=at_qqs,
        )

        if not person_id:
            await self.ctx.send.text("未找到该用户", stream_id)
            return False, "未找到", 1

        record = self.db.get_active_profile(person_id, stream_id=stream_id)
        used_stream_id = stream_id
        if not record:
            record = self.db.get_active_profile(person_id, stream_id="")
            used_stream_id = ""

        if not record:
            await self.ctx.send.text(f"「{name}」暂无画像记录", stream_id)
            return False, "无画像", 1

        days = self._days_since(record.updated_at)

        # 优先发送缓存图片
        img_path = self._get_image_path(person_id, used_stream_id, record.version)
        if img_path.exists():
            try:
                import base64
                img_b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
                await self.ctx.send.text(f"【{name}】的画像({days}天前生成,版本 {record.version})", stream_id)
                await self.ctx.send.image(img_b64, stream_id)
                return True, "查看画像(图片)", 2
            except Exception as exc:
                self.ctx.logger.debug("发送缓存图片失败: %s", exc)

        # 图片不存在时现场渲染
        if self.config.portrayal.enable_image_output:
            try:
                img_b64 = await self._render_portrait_card(
                    record.display_text, name,
                    __import__('datetime').datetime.now().strftime('%Y-%m-%d'),
                    0, '', record.mode or 'portrait',
                )
                if img_b64:
                    self._save_image_cache(img_b64, person_id, used_stream_id, record.version)
                    await self.ctx.send.text(f"【{name}】的画像({days}天前生成,版本 {record.version})", stream_id)
                    await self.ctx.send.image(img_b64, stream_id)
                    return True, "查看画像(渲染图片)", 2
            except Exception as exc:
                self.ctx.logger.debug("现场渲染图片失败,回退文字: %s", exc)

        # 最终回退:发送文字
        text = f"【{name}】的画像({days}天前生成,版本 {record.version})\n\n{record.display_text}"
        if len(text) > 3000:
            text = text[:3000] + "\n...(已截断)"
        await self.ctx.send.text(text, stream_id)
        return True, "查看画像", 2

    # ─── LLM 调用(retry + 指数退避 + 双路 provider)────────────────

    async def _call_llm_with_retry(self, system_prompt: str, user_prompt: str) -> str:
        # 每次调用前重置 usage,由具体实现填充
        self._last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        provider = (self.config.portrayal.provider or "").strip().lower()
        if provider == "deepseek":
            return await self._call_deepseek_with_retry(system_prompt, user_prompt)
        # maibot / other: use MaiBot built-in
        return await self._call_maibot_llm_with_retry(system_prompt, user_prompt)

    async def _call_deepseek_with_retry(self, system_prompt: str, user_prompt: str) -> str:
        import aiohttp

        api_key = (self.config.portrayal.deepseek_api_key or "").strip()
        base_url = (self.config.portrayal.deepseek_base_url or "https://api.deepseek.com/v1").strip().rstrip("/")
        model = (self.config.portrayal.deepseek_model or "deepseek-chat").strip()
        temperature = self.config.portrayal.temperature
        retry_times = self.config.portrayal.llm_retry_times

        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }

        for attempt in range(retry_times + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            self.ctx.logger.error("Deepseek API 返回 %d: %s", resp.status, body[:500])
                            if attempt < retry_times:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            return ""
                        data = await resp.json()
                        usage = data.get("usage") or {}
                        if isinstance(usage, dict):
                            self._last_usage = {
                                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                            }
                        choices = data.get("choices", [])
                        if choices:
                            msg = choices[0].get("message", {})
                            return (msg.get("content", "") or "").strip()
                        return ""
            except Exception as exc:
                self.ctx.logger.error("Deepseek API 调用失败 (attempt %d): %s", attempt + 1, exc)
                if attempt < retry_times:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return ""

        return ""

    async def _call_maibot_llm_with_retry(self, system_prompt: str, user_prompt: str) -> str:
        retry_times = self.config.portrayal.llm_retry_times
        temperature = self.config.portrayal.temperature

        for attempt in range(retry_times + 1):
            try:
                result = await self.ctx.llm.generate(
                    prompt=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                )
                if result.get("success"):
                    usage = result.get("usage") or {}
                    if isinstance(usage, dict):
                        self._last_usage = {
                            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                            "total_tokens": int(usage.get("total_tokens", 0) or 0),
                        }
                    return result.get("response", "").strip()
                if attempt < retry_times:
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s...
                    continue
                return ""
            except Exception as exc:
                self.ctx.logger.error("MaiBot LLM 调用失败 (attempt %d): %s", attempt + 1, exc)
                if attempt < retry_times:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return ""

        return ""

    # ─── 消息格式化(★ 标注)────────────────────────────────────────

    @staticmethod
    def _format_messages_with_marker(
        messages: list[CollectedMessage], target_user_id: str, target_name: str,
    ) -> str:
        lines: list[str] = []
        for msg in messages:
            is_target = bool(msg.user_id and target_user_id and msg.user_id == target_user_id)
            time_str = datetime.fromtimestamp(msg.timestamp).strftime("%m-%d %H:%M") if msg.timestamp else ""
            prefix = f"[{time_str}] ★{msg.user_name}:" if is_target else f"[{time_str}] {msg.user_name}:"
            lines.append(prefix + msg.text)
        return "\n".join(lines)

    # ─── 辅助方法 ────────────────────────────────────────────────────

    def _check_permission(self, user_id: str) -> bool:
        allowed = self.config.portrayal.allowed_user_ids
        if not allowed:
            return True
        return str(user_id) in [str(u) for u in allowed]

    def _is_protected(self, user_id: str) -> bool:
        protected = self.config.portrayal.protected_user_ids
        return str(user_id) in [str(u) for u in protected]

    async def _get_group_id(self, stream_id: str) -> str:
        """从 stream_id 反查 group_id。"""
        try:
            streams = await self.ctx.chat.get_group_streams()
            if isinstance(streams, dict):
                streams = streams.get("streams", [])
            elif not isinstance(streams, list):
                self.ctx.logger.warning("get_group_streams 返回意外类型: %s", type(streams))
                return ""
            self.ctx.logger.debug("stream总数: %d, 查找: %s", len(streams), stream_id)
            for s in streams:
                if isinstance(s, dict):
                    sid = s.get("stream_id", s.get("session_id", ""))
                    if sid == stream_id:
                        gid = str(s.get("group_id", "") or "")
                        self.ctx.logger.info("stream_id=%s → group_id=%s", stream_id, gid)
                        return gid
            self.ctx.logger.warning("未找到 stream_id=%s 对应的 group_id", stream_id)
        except Exception as exc:
            self.ctx.logger.warning("反查 group_id 失败: %s", exc)
        return ""

    async def _get_sender_user_id(self, message_id: str, session_id: str) -> str:
        try:
            result = await self.ctx.message.get_by_id(message_id, chat_id=session_id)
            if isinstance(result, dict):
                msg_info = result.get("message_info") or {}
                user_info = msg_info.get("user_info") or {}
                if isinstance(user_info, dict):
                    return str(user_info.get("user_id", "") or "").strip()
        except Exception as exc:
            self.ctx.logger.debug("获取消息发送者失败: message_id=%s err=%s", message_id, exc)
        return ""

    async def _detect_platform(self, session_id: str) -> str:
        return "qq"

    def _format_hook_profile_block(self, profile_text: str, is_cross_group: bool = False, source_group: str = "") -> str:
        cross_warn = ""
        if is_cross_group and source_group:
            cross_warn = f"\n⚠️ 本画像基于群 {source_group} 生成,跨群使用时可能不准确。\n"
        return (
            "【人物画像参考】\n"
            "以下内容仅作为低优先级表达参考,用于称呼、长期偏好、已知边界和回复密度。\n"
            "当前聊天记录和本次目标消息优先于画像;不要逐字复述画像,不要把画像当成本轮事实,不要基于画像推测用户心理。\n"
            f"{cross_warn}\n"
            f"{profile_text}"
        )

    def _merge_cross_group_profiles(self, records: list[ProfileRecord]) -> str:
        """将多个群的画像合并为综合版注入文本。

        策略:取最新画像为主体,其他群的画像作为补充标注。
        不调 LLM,纯文本拼接,避免额外 token 开销。
        """
        if not records:
            return ""
        if len(records) == 1:
            return self._format_hook_profile_block(
                records[0].profile_text.strip(),
                is_cross_group=True,
                source_group=records[0].source_group,
            )
        primary = records[0]  # 最新
        parts = [primary.profile_text.strip()]
        # 其他群的画像作为补充
        for r in records[1:]:
            # 只取 profile_text 中的【性格标签】等核心部分,避免太长
            text = r.profile_text.strip()
            if text:
                source = r.source_group or "未知群"
                parts.append(f"\n〔补充 · 基于群 {source}〕\n{text}")
        merged = "\n".join(parts)
        # 限制总长度
        if len(merged) > 1200:
            merged = merged[:1200].rstrip() + "..."
        return (
            "【人物画像参考】\n"
            "以下内容仅作为低优先级表达参考,用于称呼、长期偏好、已知边界和回复密度。\n"
            "当前聊天记录和本次目标消息优先于画像;不要逐字复述画像,不要把画像当成本轮事实,不要基于画像推测用户心理。\n"
            f"\n⚠️ 本画像基于多群聚合({len(records)} 个群),跨群使用时可能不准确。\n\n"
            f"{merged}"
        )

    # ─── 群分析 ────────────────────────────────────────────────────

    def _init_group_analyzer(self) -> None:
        if self._group_analyzer is not None:
            return
        self._group_analyzer = GroupAnalyzer(
            deepseek_api_key=self.config.portrayal.deepseek_api_key,
            deepseek_base_url=self.config.portrayal.deepseek_base_url,
            deepseek_model=self.config.portrayal.deepseek_model,
        )

    async def _execute_group_analysis(self, kwargs: dict) -> tuple:
        """分析当前群的话题、标签和用户关系。

        优先使用群缓存消息,否则新拉取一轮。
        """
        stream_id = kwargs.get("stream_id", "")
        if not stream_id:
            await self.ctx.send.text("无法获取群聊信息", stream_id or "")
            return False, "无 stream_id", 1

        group_id = await self._get_group_id(stream_id)
        if not group_id:
            await self.ctx.send.text("无法获取群号", stream_id)
            return False, "无 group_id", 1

        await self.ctx.send.text(f"正在分析群 {group_id} 的聊天内容...", stream_id)

        self._init_group_analyzer()

        # 1. 获取消息:缓存优先
        messages = self.collector.get_group_messages(group_id)
        if messages and len(messages) > 50:
            self.ctx.logger.info("群分析: 缓存命中 group=%s msgs=%d", group_id, len(messages))
        else:
            self.ctx.logger.info("群分析: 缓存未命中 group=%s,拉取新消息", group_id)
            result = await self.collector.collect(
                api_call_fn=self.ctx.api.call,
                group_id=group_id,
                target_user_id="0",  # 无目标用户,拉全量
            )
            messages = result.all_messages

        if not messages:
            await self.ctx.send.text("未拉取到消息,无法分析", stream_id)
            return False, "无消息", 1

        # 2. 统计分析
        analysis = await self._group_analyzer.analyze(messages, group_id)

        # 3. 保存
        self.db.save_group_analysis(
            source_group=group_id,
            tags=analysis.get("tags", []),
            hot_topics=analysis.get("hot_topics", []),
            top_words=analysis.get("top_words", []),
            user_catchphrases=analysis.get("user_catchphrases", {}),
            user_relations=analysis.get("user_relations", {}),
            summary=analysis.get("summary", ""),
            message_count=analysis.get("message_count", 0),
            user_count=analysis.get("user_count", 0),
        )

        # 4. 发送结果
        tags_str = " ".join(analysis.get("tags", [])[:8])
        topics = analysis.get("hot_topics", [])[:5]
        topics_str = "\n".join(
            f"  {i+1}. {t.get('name', '?')} ({t.get('weight', 0)})"
            for i, t in enumerate(topics)
        )
        summary = analysis.get("summary", "")
        reply = (
            f"📊 群分析报告 ({analysis.get('user_count', 0)} 人, {analysis.get('message_count', 0)} 条消息)\n\n"
            f"🏷️ 标签: {tags_str or '暂无'}\n\n"
            f"🔥 热门话题:\n{topics_str or '  暂无'}\n\n"
            f"📝 {summary}"
        )
        await self.ctx.send.text(reply, stream_id)
        return True, f"群分析完成: {group_id}", 2

    async def _resolve_target(self, platform: str, sender_user_id: str, target_text: str, at_qqs: list[str] | None = None) -> tuple[str, str, str]:
        """解析目标用户。

        优先级:原始 @ QQ 号 > target_text 模式匹配 > 自分析。
        """
        target_text = target_text.strip()
        at_qqs = at_qqs or []

        # 空目标 + 无 @ → 分析发送者
        if not target_text and not at_qqs:
            if not sender_user_id:
                return "", "", ""
            person_id = await self._safe_get_person_id(platform, sender_user_id)
            name = await self._safe_get_person_name(person_id) or sender_user_id
            return sender_user_id, person_id, name

        # 原始 @ 中有 QQ 号 → 直接用(astrbot 同款,不依赖 Person 系统)
        if at_qqs:
            qq = at_qqs[0]
            person_id = await self._safe_get_person_id(platform, qq)
            name = await self._safe_get_person_name(person_id) or qq
            if name.startswith("未知用户"):
                name = await self._fetch_qq_nickname(qq)
            return qq, person_id, name

        # 去掉 @ 前缀(target_text 可能是 "@昵称" 或 "@QQ号")
        if target_text.startswith("@"):
            target_text = target_text[1:].strip()

        # 看起来像 QQ 号:直接用
        if target_text.isdigit() and 5 <= len(target_text) <= 12:
            user_id = target_text
            person_id = await self._safe_get_person_id(platform, user_id)
            name = await self._safe_get_person_name(person_id) or user_id
            return user_id, person_id, name

        # 按名字查找
        person_id = await self._safe_get_person_id_by_name(target_text)
        if person_id:
            name = await self._safe_get_person_name(person_id) or target_text
            user_id = await self._safe_get_person_value(person_id, "user_id") or ""
            if not user_id:
                user_id = await self._get_user_id_by_person_id(person_id)
            if not user_id:
                self.ctx.logger.warning("按名字 %s 找到 person_id=%s 但无 user_id", target_text, person_id)
            return user_id, person_id, name

        # 按 user_nickname 查找(@ 渲染的昵称可能不等于 person_name)
        pid, uid = await self._find_person_by_nickname(platform, target_text)
        if pid:
            name = await self._safe_get_person_name(pid) or target_text
            return uid, pid, name

        # 按 platform+user_id 查找(target_text 可能本身就是 user_id)
        # 注意:_safe_get_person_id 会对不存在的用户创建 Person 条目,
        # 前面的 5 层查找(at_qqs → QQ号 → person_name → user_nickname → 同名user_id)
        # 已经覆盖了所有合理场景,此路径只用于真正的纯数字但太短的 user_id。
        if target_text.isdigit():
            person_id = await self._safe_get_person_id(platform, target_text)
            if person_id:
                name = await self._safe_get_person_name(person_id) or target_text
                if name.startswith("未知用户"):
                    self.ctx.logger.warning("无效 user_id: %s,Person 未注册", target_text)
                    return target_text, "", target_text
                user_id = await self._safe_get_person_value(person_id, "user_id") or ""
                if not user_id:
                    user_id = await self._get_user_id_by_person_id(person_id) or target_text
                return user_id, person_id, name

        self.ctx.logger.warning("无法解析目标: target_text=%s", target_text)
        return target_text, "", target_text

    async def _safe_get_person_id(self, platform: str, user_id: str) -> str:
        try:
            result = await self.ctx.person.get_id(platform, user_id)
            if isinstance(result, dict):
                return str(result.get("person_id", "") or "").strip()
            return str(result or "").strip()
        except Exception:
            return ""

    async def _safe_get_person_id_by_name(self, name: str) -> str:
        try:
            result = await self.ctx.person.get_id_by_name(name)
            if isinstance(result, dict):
                return str(result.get("person_id", "") or "").strip()
            return str(result or "").strip()
        except Exception:
            return ""

    async def _safe_get_person_name(self, person_id: str) -> str:
        return await self._safe_get_person_value(person_id, "person_name")

    async def _safe_get_person_value(self, person_id: str, field_name: str) -> str:
        if not person_id:
            return ""
        try:
            result = await self.ctx.person.get_value(person_id, field_name)
            if isinstance(result, dict):
                return str(result.get("value", "") or "").strip()
            return str(result or "").strip()
        except Exception:
            return ""

    async def _get_user_id_by_person_id(self, person_id: str) -> str:
        """直查 PersonInfo 表获取 user_id(绕过 Person 类对未知用户的短路径)。"""
        if not person_id:
            return ""
        try:
            result = await self.ctx.database.get(
                "PersonInfo",
                filters={"person_id": person_id},
                single_result=True,
            )
            if isinstance(result, dict):
                return str(result.get("user_id", "") or "").strip()
            return ""
        except Exception as exc:
            self.ctx.logger.debug("直查 PersonInfo.user_id 失败: %s", exc)
            return ""

    async def _find_person_by_nickname(self, platform: str, nickname: str) -> tuple[str, str]:
        """按 user_nickname 查 PersonInfo,返回 (person_id, user_id)。

        @ 渲染的昵称来自 PersonInfo.user_nickname,可能不等于 person_name。
        """
        if not nickname:
            return "", ""
        try:
            result = await self.ctx.database.get(
                "PersonInfo",
                filters={"user_nickname": nickname, "platform": platform},
                single_result=True,
            )
            if isinstance(result, dict):
                return (
                    str(result.get("person_id", "") or "").strip(),
                    str(result.get("user_id", "") or "").strip(),
                )
            return "", ""
        except Exception as exc:
            self.ctx.logger.debug("按 nickname 查 PersonInfo 失败: %s", exc)
            return "", ""

    @staticmethod
    def _parse_llm_output(response: str, date: str, msg_count: int, group_id: str) -> tuple[str, str]:
        display_match = re.search(r"===展示版===(.*?)(?:===注入版===|$)", response, re.DOTALL)
        override_match = re.search(r"===注入版===(.*?)$", response, re.DOTALL)

        display_text = display_match.group(1).strip() if display_match else response.strip()
        override_text = override_match.group(1).strip() if override_match else ""

        if not override_text:
            override_text = display_text

        if len(override_text) > OVERRIDE_MAX_CHARS:
            override_text = override_text[:OVERRIDE_MAX_CHARS].rstrip() + "..."

        if "[画像生成时间:" not in override_text:
            header = f"[画像生成时间: {date} | 基于群: {group_id} 最近 {msg_count} 条消息 | 来源: portrayal_plugin]\n\n"
            override_text = header + override_text

        return display_text, override_text

    @staticmethod
    def _safe_format_prompt(template: str, **kwargs) -> str:
        """安全替换 prompt 模板中的 placeholder。

        用 str.replace 逐个替换,不用 str.format(),
        避免聊天记录/LLM 输出中的 { } 导致 KeyError。
        """
        result = template
        for key, value in kwargs.items():
            result = result.replace("{" + key + "}", str(value))
        return result

    @staticmethod
    def _extract_at_qqs(raw_segments: list[dict]) -> list[str]:
        """从原始消息段中提取 @ 的 QQ 号列表。

        MaiBot 的 AtComponent 序列化格式:
        {"type": "at", "data": {"target_user_id": "123456789", ...}}。
        """
        qqs: list[str] = []
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") != "at":
                continue
            data = seg.get("data", {})
            if isinstance(data, dict):
                qq = str(data.get("target_user_id", "") or "").strip()
                if qq:
                    qqs.append(qq)
        return qqs

    async def _fetch_qq_nickname(self, qq: str) -> str:
        """通过 NapCat API 获取 QQ 号对应的昵称。"""
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.account.get_stranger_info",
                user_id=int(qq),
                no_cache=False,
            )
            if isinstance(result, dict):
                return str(result.get("nickname", "") or qq).strip()
        except Exception as exc:
            self.ctx.logger.debug("获取 QQ %s 昵称失败: %s", qq, exc)
        return qq

    async def _fetch_group_card_and_nick(self, group_id: str, user_id: str) -> tuple[str, str]:
        """通过 NapCat API 获取群成员的群名片和 QQ 昵称。

        返回 (card, nickname)。
        """
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
            )
            data = result
            if isinstance(result, dict) and "data" in result and isinstance(result["data"], dict):
                data = result["data"]
            if isinstance(data, dict):
                card = str(data.get("card", "") or "").strip()
                nick = str(data.get("nickname", "") or "").strip()
                return card, nick
        except Exception as exc:
            self.ctx.logger.debug("获取群名片失败: group=%s user=%s err=%s", group_id, user_id, exc)
        return "", ""

    async def _search_friends(self, query: str, group_id: str = "") -> list[dict]:
        """从 NapCat 群成员列表搜索,返回匹配的用户列表。

        如果有 group_id,从该群成员列表搜;否则从好友列表搜。
        返回 [{user_id, nickname}, ...]
        """
        try:
            members = []
            if group_id:
                self.ctx.logger.info("搜索群成员: group_id=%s query=%s", group_id, query)
                result = await self.ctx.api.call(
                    "adapter.napcat.group.get_group_member_list",
                    group_id=int(group_id),
                    no_cache=False,
                )
                self.ctx.logger.info("群成员 API 返回类型: %s", type(result).__name__)
                # typed API 可能直接返回 list,也可能包在 dict 里
                if isinstance(result, list):
                    members = result
                elif isinstance(result, dict):
                    # 可能是 {"data": [...]} 或 {"result": [...]}
                    members = result.get("data", result.get("result", []))
                    if not isinstance(members, list):
                        members = []
                else:
                    self.ctx.logger.warning("群成员 API 返回非预期类型: %s", type(result))
            else:
                result = await self.ctx.api.call(
                    "adapter.napcat.account.get_friend_list",
                )
                if isinstance(result, dict):
                    members = result.get("data", result)
                if not isinstance(members, list):
                    members = []

            self.ctx.logger.info("群成员数量: %d", len(members))
            q = query.lower().strip()
            matched = []
            for m in members:
                if not isinstance(m, dict):
                    continue
                uid = str(m.get("user_id", m.get("user_id", "")) or "")
                nick = str(m.get("nickname", "") or "")  # QQ 昵称
                card = str(m.get("card", "") or "")  # 群名片
                if not q or q in uid.lower() or q in nick.lower() or q in card.lower():
                    matched.append({
                        "user_id": uid,
                        "nickname": nick or card or uid,
                        "card": card,
                        "title": str(m.get("title", "") or ""),  # 头衔
                    })
                if len(matched) >= 30:
                    break
            self.ctx.logger.info("匹配成员数: %d", len(matched))
            return matched
        except Exception as exc:
            self.ctx.logger.warning("搜索群成员/好友失败: %s", exc, exc_info=True)
            return []

    async def _render_portrait_card(
        self, display_text: str, target_name: str, date: str, msg_count: int, group_id: str, mode: str,
    ) -> str:
        """将展示版画像渲染为 HTML 卡片图,返回 base64 字符串。"""
        is_yin = mode == "yin_yang"
        theme_color = "#c0392b" if is_yin else "#2c6e8f"
        theme_bg = "#fdf2f2" if is_yin else "#f0f6f9"
        accent_bg = "rgba(192,57,43,.06)" if is_yin else "rgba(44,110,143,.06)"
        mode_label = "阴阳画像" if is_yin else "人物画像"

        safe_name = target_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe_group = str(group_id).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        meta = f"群 {safe_group} · {msg_count} 条消息 · {date}"

        safe_text = display_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe_text = safe_text.replace("\n", "<br>")
        safe_text = re.sub(r"【(.+?)】", r'<span class="sec-title">【\1】</span>', safe_text)
        safe_text = safe_text.replace('🟢', '<span class="diff-add">🟢</span>')
        safe_text = safe_text.replace('🔴', '<span class="diff-del">🔴</span>')
        safe_text = safe_text.replace('🟡', '<span class="diff-chg">🟡</span>')
        safe_text = safe_text.replace('⚪', '<span class="diff-same">⚪</span>')

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600&display=swap');
body {{
  font-family: "Noto Sans SC", "Microsoft YaHei", sans-serif;
  background: {theme_bg};
  padding: 28px;
  width: 540px;
}}
.card {{
  background: #fff;
  border-radius: 14px;
  padding: 0;
  box-shadow: 0 4px 24px rgba(0,0,0,.06);
  overflow: hidden;
}}
.card-banner {{
  background: linear-gradient(135deg, {theme_color}, {theme_color}cc);
  color: #fff;
  padding: 18px 24px;
}}
.card-banner .mode-tag {{
  display: inline-block;
  font-size: 11px;
  background: rgba(255,255,255,.2);
  padding: 2px 10px;
  border-radius: 10px;
  margin-bottom: 6px;
}}
.card-banner h1 {{
  font-size: 19px;
  font-weight: 600;
}}
.card-banner .meta {{
  font-size: 11px;
  opacity: .8;
  margin-top: 4px;
}}
.card-body {{
  padding: 20px 24px;
  font-size: 14px;
  line-height: 1.85;
  color: #3a3732;
}}
.sec-title {{
  color: {theme_color};
  font-weight: 600;
  display: inline-block;
  margin-top: 6px;
}}
.diff-add {{ background:#e8f5e9; padding:1px 4px; border-radius:3px; }}
.diff-del {{ background:#fce4ec; padding:1px 4px; border-radius:3px; }}
.diff-chg {{ background:#fff8e1; padding:1px 4px; border-radius:3px; }}
.diff-same {{ opacity:.7; }}
</style>
</head><body>
<div class="card">
  <div class="card-banner">
    <span class="mode-tag">{mode_label}</span>
    <h1>🎭 {safe_name}</h1>
    <div class="meta">{meta}</div>
  </div>
  <div class="card-body">{safe_text}</div>
</div>
</body></html>"""

        result = await self.ctx.render.html2png(
            html=html,
            selector="body",
            viewport={"width": 540, "height": 200},
            full_page=True,
            device_scale_factor=1.5,
        )
        if isinstance(result, dict):
            inner = result.get("result", result)
            if isinstance(inner, dict):
                img_b64 = inner.get("image_base64", "")
                if img_b64:
                    return img_b64
        return ""

    def _safe_add_gen_log(self, *, operator_user_id, target_user_id, target_person_id,
                          target_name, stream_id, source_group, mode, scanned_msgs,
                          target_msgs, usage, duration_ms, success, error, record_id, version) -> None:
        """写生成日志,任何异常不影响主流程。"""
        try:
            usage = usage or {}
            self.db.add_generation_log(
                operator_user_id=str(operator_user_id or ""),
                operator_name="",
                target_user_id=str(target_user_id or ""),
                target_person_id=str(target_person_id or ""),
                target_name=str(target_name or ""),
                stream_id=str(stream_id or ""),
                source_group=str(source_group or ""),
                mode=str(mode or "portrait"),
                scanned_msgs=int(scanned_msgs or 0),
                target_msgs=int(target_msgs or 0),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                duration_ms=int(duration_ms or 0),
                success=bool(success),
                error=str(error or "")[:500],
                record_id=int(record_id or 0),
                version=int(version or 0),
            )
        except Exception as exc:
            self.ctx.logger.debug("写生成日志失败: %s", exc)

    async def _resolve_group_name(self, group_id: str) -> str:
        """拉群名并写入缓存;NapCat 不可用时降级为群号。"""
        if not group_id:
            return ""
        try:
            result = await self.ctx.api.call(
                "adapter.napcat.group.get_group_info",
                group_id=int(group_id),
            )
            name = ""
            member_count = 0
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    name = str(data.get("group_name", "") or "").strip()
                    member_count = int(data.get("member_count", 0) or 0)
            if name:
                try:
                    self.db.upsert_group_meta(group_id, name, member_count)
                except Exception as exc:
                    self.ctx.logger.debug("写群名缓存失败: %s", exc)
                return name
        except Exception as exc:
            self.ctx.logger.debug("获取群 %s 名称失败: %s", group_id, exc)
        return group_id

    @staticmethod
    def _days_since(timestamp: float) -> int:
        if not timestamp:
            return 999
        try:
            return max(0, int((time.time() - float(timestamp)) / 86400))
        except (TypeError, ValueError):
            return 999


def create_plugin():
    return PortrayalPlugin()
