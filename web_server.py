"""独立 WebUI 服务器 — aiohttp REST API + 静态文件。

启动在 config.portrayal.webui_port（默认 8089）。
无认证，仅限本地网络使用。
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any, Optional

from aiohttp import web


_ALLOWED_PROMPT_NAMES = frozenset({
    "portrait_system", "portrait_user",
    "yin_yang_system", "yin_yang_user",
})

_MIME_MAP: dict[str, str] = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ico": "image/x-icon",
    ".txt": "text/plain",
}


class PortrayalWebServer:
    """人物画像 WebUI 服务器。"""

    def __init__(
        self,
        *,
        db,
        prompts_dir: Path,
        prompts: dict[str, str],
        logger,
        port: int = 8089,
        host: str = "127.0.0.1",
        auth_token: str = "",
        image_cache_dir: Path = None,
        collect_callback = None,
        search_friends_callback = None,
    ) -> None:
        self._db = db
        self._prompts_dir = Path(prompts_dir)
        self._prompts: dict[str, str] = dict(prompts)
        self._logger = logger
        self._host = host
        self._port = port
        self._auth_token = (auth_token or "").strip()
        self._dist_dir = Path(__file__).resolve().parent / "webui" / "dist"
        self._image_cache_dir = Path(image_cache_dir) if image_cache_dir else None
        self._collect_callback = collect_callback
        self._search_friends_callback = search_friends_callback
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    # ─── 生命周期 ──────────────────────────────────────────────────

    async def start(self) -> None:
        if self._runner is not None:
            return
        middlewares = []
        if self._auth_token:
            middlewares.append(self._auth_middleware)
        app = web.Application(client_max_size=8 * 1024 * 1024, middlewares=middlewares)
        self._register_routes(app)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        auth_note = "启用 token 认证" if self._auth_token else "无认证"
        self._logger.info("Portrayal WebUI 已启动: http://%s:%d (%s)", self._host, self._port, auth_note)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """简单 token 认证：静态页放行，API 校验 Authorization 或 ?token=。"""
        path = request.path
        if path.startswith("/api/"):
            token = ""
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            if not token:
                token = request.query.get("token", "").strip()
            if token != self._auth_token:
                return web.json_response({"success": False, "error": "未授权"}, status=401)
        return await handler(request)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _register_routes(self, app: web.Application) -> None:
        r = app.router
        # ── 画像列表 ──
        r.add_get("/api/portrayal/profiles", self._list_profiles)

        # ── 画像详情/历史（person/ 前缀必须在 {person_id} 之前注册）──
        r.add_get("/api/portrayal/profiles/person/{person_id}", self._get_profile_by_person)

        # ── 画像操作（person 级）──
        r.add_delete("/api/portrayal/profiles/person/{person_id}", self._delete_person)
        r.add_post("/api/portrayal/profiles/{person_id}/activate/{record_id}", self._activate_version)
        r.add_post("/api/portrayal/profiles/{person_id}/deactivate", self._deactivate_profile)

        # ── 画像操作（record 级）──
        r.add_get("/api/portrayal/profiles/{person_id}", self._get_profile)
        r.add_delete("/api/portrayal/profiles/{record_id}", self._delete_profile)
        r.add_put("/api/portrayal/profiles/{record_id}/text", self._update_text)
        r.add_put("/api/portrayal/profiles/{record_id}/display-text", self._update_display_text)
        r.add_put("/api/portrayal/profiles/{record_id}/tags", self._update_tags)
        r.add_post("/api/portrayal/profiles/{record_id}/lock", self._lock_profile)
        r.add_post("/api/portrayal/profiles/{record_id}/unlock", self._unlock_profile)

        # ── 采集（WebUI 触发）──
        r.add_post("/api/portrayal/collect", self._collect_profile)

        # ── 用户搜索 ──
        r.add_get("/api/portrayal/search-user", self._search_user)

        # ── 回收站 ──
        r.add_get("/api/portrayal/recycle", self._list_recycle)
        r.add_post("/api/portrayal/recycle/{record_id}/restore", self._restore_profile)
        r.add_delete("/api/portrayal/recycle/{record_id}", self._purge_profile)
        r.add_delete("/api/portrayal/recycle", self._empty_recycle)

        # ── 生成日志 & 分析 ──
        r.add_get("/api/portrayal/logs", self._list_logs)
        r.add_get("/api/portrayal/analytics", self._get_analytics)

        # ── 群组 & 标签 ──
        r.add_get("/api/portrayal/groups", self._list_groups)
        r.add_get("/api/portrayal/tags", self._list_tags)

        # ── 群分析 ──
        r.add_get("/api/portrayal/group-analysis/{group_id}", self._get_group_analysis)

        # ── Prompt ──
        r.add_get("/api/portrayal/prompts", self._list_prompts)
        r.add_get("/api/portrayal/prompts/{name}", self._get_prompt)
        r.add_put("/api/portrayal/prompts/{name}", self._update_prompt)

        # ── 配置 & 统计 ──
        r.add_get("/api/portrayal/config", self._get_config)
        r.add_put("/api/portrayal/config", self._update_config)
        r.add_get("/api/portrayal/stats", self._get_stats)

        # ── 静态文件 ──
        r.add_get("/", self._serve_index)
        r.add_get("/{tail:(?!api/).*}", self._serve_static)

    # ─── 工具方法 ──────────────────────────────────────────────────

    @staticmethod
    def _ok(data: Any = None) -> web.Response:
        return web.json_response({"success": True, "data": data})

    @staticmethod
    def _err(msg: str, status: int = 400) -> web.Response:
        return web.json_response({"success": False, "error": msg}, status=status)

    @staticmethod
    def _to_int(val: str | None, default: int = 0) -> int:
        if val is None or val == "":
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _delete_image_file(record, cache_dir=None) -> None:
        """删除与画像记录关联的图片缓存文件。"""
        try:
            import re as _re
            from pathlib import Path
            person_id = getattr(record, 'person_id', '') or ''
            stream_id = getattr(record, 'stream_id', '') or ''
            version = getattr(record, 'version', 0) or 0
            if not person_id:
                return
            safe_id = _re.sub(r'[^a-zA-Z0-9_]', '_', person_id)
            safe_stream = _re.sub(r'[^a-zA-Z0-9_]', '_', stream_id)
            filename = f"{safe_id}_{safe_stream}_v{version}.png"
            # 用传入的缓存目录，或回退到几个常见路径
            dirs = []
            if cache_dir:
                dirs.append(Path(cache_dir))
            dirs.extend([
                Path('data/plugins/rc.portrayal/portrayal_images'),
                Path.home() / '.maibot' / 'data' / 'portrayal_images',
            ])
            for base in dirs:
                img_path = base / filename
                if img_path.exists():
                    img_path.unlink()
                    break
        except Exception:
            pass  # 图片删除失败不影响主流程

    @staticmethod
    def _to_float(val: str | None, default: float | None = None) -> float | None:
        if val is None or val == "":
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    # ─── 采集（WebUI 触发）──────────────────────────────────────

    async def _collect_profile(self, request: web.Request) -> web.Response:
        """WebUI 触发的画像采集端点。

        接收 JSON body: {person_id, user_id, mode, force}
        如果 plugin 注入了 collect_callback，则执行采集；否则返回提示。
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "无效的 JSON body"}, status=400)
        person_id = str(body.get("person_id", "") or "")
        user_id = str(body.get("user_id", "") or "")
        mode = str(body.get("mode", "portrait") or "portrait")
        force = bool(body.get("force", False))
        group_id = str(body.get("group_id", "") or "")

        if self._collect_callback:
            try:
                result = await self._collect_callback(
                    person_id=person_id, user_id=user_id, mode=mode, force=force, group_id=group_id,
                )
                if result.get("success"):
                    return web.json_response({"success": True, "data": result})
                else:
                    return web.json_response({"success": False, "error": result.get("error", "采集失败")})
            except Exception as exc:
                self._logger.error("WebUI collect 回调失败: %s", exc, exc_info=True)
                return web.json_response({"success": False, "error": f"采集异常: {exc}"})
        else:
            return web.json_response({
                "success": False,
                "error": "WebUI 采集未接入，请在群聊中使用 +画像 指令",
                "person_id": person_id,
                "user_id": user_id,
            })

    # ─── 用户搜索 ──────────────────────────────────────────────────

    async def _search_user(self, request: web.Request) -> web.Response:
        """搜索群成员，返回匹配用户及各群画像状态。

        数据源：1) NapCat 群成员列表（通过 search_friends_callback 传 group_id） 2) 画像库兜底
        """
        try:
            query = request.query.get("q", "").strip()
            group_id = request.query.get("group_id", "").strip()
            if not query:
                return self._ok({"items": [], "groups": []})

            results = []
            seen_qqs = set()

            # 1. 从 NapCat 群成员列表搜索
            if self._search_friends_callback:
                try:
                    friends = await self._search_friends_callback(query, group_id=group_id)
                    self._logger.info("search_friends_callback 返回 %d 条", len(friends) if friends else 0)
                    for f in friends:
                        qq = str(f.get("user_id", ""))
                        if qq and qq not in seen_qqs:
                            seen_qqs.add(qq)
                            # 查画像库中该用户的各群画像状态
                            group_profiles = []
                            pid = ""
                            if qq:
                                db_results = self._db.search_users(qq, limit=50)
                                for r in db_results:
                                    if r.get("user_id") == qq:
                                        pid = r.get("person_id", "")
                                        break
                            if pid:
                                group_profiles = self._db.get_user_group_profiles(pid)
                            results.append({
                                "person_id": pid,
                                "user_id": qq,
                                "person_name": f.get("nickname", qq),
                                "nickname": f.get("nickname", ""),
                                "card": f.get("card", ""),
                                "group_profiles": group_profiles,
                            })
                except Exception as exc:
                    self._logger.warning("搜索群成员失败: %s", exc)

            # 2. 如果没找到，也搜画像库兜底
            if not results:
                db_results = self._db.search_users(query, limit=20)
                for r in db_results:
                    qq = r.get("user_id", "")
                    key = qq or r.get("person_id", "")
                    if key and key not in seen_qqs:
                        seen_qqs.add(key)
                        pid = r.get("person_id", "")
                        group_profiles = self._db.get_user_group_profiles(pid) if pid else []
                        results.append({
                            "person_id": pid,
                            "user_id": qq,
                            "person_name": r.get("person_name", ""),
                            "group_profiles": group_profiles,
                        })

            return self._ok({"items": results})
        except Exception as exc:
            self._logger.error("用户搜索失败: %s", exc, exc_info=True)
            return self._err(str(exc), 500)

    # ─── 画像列表 ──────────────────────────────────────────────────

    async def _list_profiles(self, request: web.Request) -> web.Response:
        try:
            q = request.query

            # 解析标签
            tags_str = q.get("tags", "")
            tags_list: Optional[list[str]] = None
            if tags_str:
                tags_list = [t.strip() for t in tags_str.split(",") if t.strip()]

            # 分页
            page = max(1, self._to_int(q.get("page", "1"), 1))
            page_size_raw = self._to_int(q.get("page_size", "50"), 50)
            page_size = max(1, min(200, page_size_raw))
            offset = (page - 1) * page_size

            # locked 参数：存在且非空时才传值
            locked_raw = q.get("locked")
            locked: Optional[int] = None
            if locked_raw is not None and locked_raw != "":
                locked = self._to_int(locked_raw, 1)

            # is_active 参数：存在且非空时才传值，否则不筛选
            is_active_raw = q.get("is_active")
            is_active: Optional[int] = None
            if is_active_raw is not None and is_active_raw != "" and is_active_raw != "all":
                is_active = self._to_int(is_active_raw, 1)

            # 日期范围
            date_from = self._to_float(q.get("date_from"))
            date_to = self._to_float(q.get("date_to"))

            # 排序
            sort_raw = q.get("sort_by", "updated_at")
            sort_order_raw = q.get("sort_order", "desc")
            sort_by = sort_raw if sort_raw in (
                "updated_at", "created_at", "person_name", "version", "mode", "msg_count",
            ) else "updated_at"
            sort_order = sort_order_raw if sort_order_raw in ("asc", "desc") else "desc"

            records, total = self._db.list_profiles(
                search=q.get("search", ""),
                source_group=q.get("source_group", ""),
                mode=q.get("mode", ""),
                tags=tags_list,
                locked=locked,
                is_active=is_active,
                date_from=date_from,
                date_to=date_to,
                sort_by=sort_by,
                sort_order=sort_order,
                offset=offset,
                limit=page_size,
            )

            total_pages = (total + page_size - 1) // page_size if total > 0 else 0

            return self._ok({
                "items": [r.to_dict() for r in records],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            })
        except Exception as exc:
            self._logger.error("WebUI list_profiles 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 画像详情 & 历史 ───────────────────────────────────────────

    async def _get_profile(self, request: web.Request) -> web.Response:
        """按 person_id 获取详情 + 历史。"""
        try:
            person_id = request.match_info.get("person_id", "")
            if not person_id:
                return self._err("缺少 person_id")
            stream_id = request.query.get("stream_id", "")
            source_group = request.query.get("source_group", "")
            if source_group and not stream_id:
                active = self._db.get_active_profile_by_group(person_id, source_group)
                history = self._db.get_history_by_group(person_id, source_group)
            else:
                active = self._db.get_active_profile(person_id, stream_id)
                history = self._db.get_history(person_id, stream_id)
            return self._ok({
                "active": active.to_dict() if active else None,
                "history": [r.to_dict() for r in history],
            })
        except Exception as exc:
            self._logger.error("WebUI get_profile 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _get_profile_by_person(self, request: web.Request) -> web.Response:
        """按 person_id 查全部画像（与 _get_profile 逻辑相同，路由别名）。"""
        return await self._get_profile(request)

    # ─── 激活 / 停用 ───────────────────────────────────────────────

    async def _activate_version(self, request: web.Request) -> web.Response:
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            record = self._db.activate_version(record_id)
            if record is None:
                return self._err("记录不存在", 404)
            return self._ok(record.to_dict())
        except Exception as exc:
            self._logger.error("WebUI activate_version 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _deactivate_profile(self, request: web.Request) -> web.Response:
        try:
            person_id = request.match_info.get("person_id", "")
            if not person_id:
                return self._err("缺少 person_id")
            count = self._db.deactivate(person_id)
            return self._ok({"deactivated": count})
        except Exception as exc:
            self._logger.error("WebUI deactivate 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 删除 ──────────────────────────────────────────────────────

    async def _delete_profile(self, request: web.Request) -> web.Response:
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            # 先查记录拿图片信息
            record = self._db.get_profile_by_id(record_id)
            ok = self._db.delete_profile(record_id)
            if not ok:
                return self._err("记录不存在", 404)
            # 删除关联图片缓存
            if record:
                self._delete_image_file(record, self._image_cache_dir)
            return self._ok({"deleted_id": record_id})
        except Exception as exc:
            self._logger.error("WebUI delete_profile 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _delete_person(self, request: web.Request) -> web.Response:
        try:
            person_id = request.match_info.get("person_id", "")
            if not person_id:
                return self._err("缺少 person_id")
            stream_id = request.query.get("stream_id", "")
            # 先查所有记录拿图片信息
            records = self._db.get_history(person_id, stream_id)
            count = self._db.delete_person_profiles(person_id, stream_id)
            # 删除关联图片缓存
            for r in (records or []):
                self._delete_image_file(r, self._image_cache_dir)
            return self._ok({"deleted_count": count})
        except Exception as exc:
            self._logger.error("WebUI delete_person 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 编辑 ──────────────────────────────────────────────────────

    async def _update_text(self, request: web.Request) -> web.Response:
        """更新注入版文本（profile_text）。"""
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            body = await request.json()
            text = str(body.get("profile_text") or "")
            record = self._db.update_profile_text(record_id, text)
            if record is None:
                return self._err("记录不存在", 404)
            return self._ok(record.to_dict())
        except Exception as exc:
            self._logger.error("WebUI update_text 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _update_display_text(self, request: web.Request) -> web.Response:
        """更新展示版文本（display_text）。"""
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            body = await request.json()
            text = str(body.get("display_text") or "")
            record = self._db.update_display_text(record_id, text)
            if record is None:
                return self._err("记录不存在", 404)
            return self._ok(record.to_dict())
        except Exception as exc:
            self._logger.error("WebUI update_display_text 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _update_tags(self, request: web.Request) -> web.Response:
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            body = await request.json()
            tags = body.get("tags")
            if not isinstance(tags, list):
                return self._err("tags 必须是数组")
            record = self._db.update_tags(record_id, [str(t).strip() for t in tags if str(t).strip()])
            if record is None:
                return self._err("记录不存在", 404)
            return self._ok(record.to_dict())
        except Exception as exc:
            self._logger.error("WebUI update_tags 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 锁定 / 解锁 ───────────────────────────────────────────────

    async def _lock_profile(self, request: web.Request) -> web.Response:
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            record = self._db.set_locked(record_id, True)
            if record is None:
                return self._err("记录不存在", 404)
            return self._ok(record.to_dict())
        except Exception as exc:
            self._logger.error("WebUI lock_profile 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _unlock_profile(self, request: web.Request) -> web.Response:
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            record = self._db.set_locked(record_id, False)
            if record is None:
                return self._err("记录不存在", 404)
            return self._ok(record.to_dict())
        except Exception as exc:
            self._logger.error("WebUI unlock_profile 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 群组 & 标签 ───────────────────────────────────────────────

    async def _list_groups(self, request: web.Request) -> web.Response:
        try:
            groups = self._db.list_groups()
            meta = {}
            try:
                meta = self._db.list_group_meta()
            except Exception:
                meta = {}
            enriched = []
            for g in sorted(groups):
                m = meta.get(str(g)) or {}
                enriched.append({
                    "group_id": str(g),
                    "group_name": str(m.get("group_name", "") or ""),
                    "member_count": int(m.get("member_count", 0) or 0),
                })
            return self._ok({"groups": sorted(groups), "groups_meta": enriched})
        except Exception as exc:
            self._logger.error("WebUI list_groups 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 回收站 ─────────────────────────────────────────

    async def _list_recycle(self, request: web.Request) -> web.Response:
        try:
            q = request.query
            page = max(1, self._to_int(q.get("page", "1"), 1))
            page_size = max(1, min(200, self._to_int(q.get("page_size", "50"), 50)))
            offset = (page - 1) * page_size
            records, total = self._db.list_profiles(
                is_active=None, include_deleted=True,
                offset=offset, limit=page_size,
            )
            total_pages = (total + page_size - 1) // page_size if total > 0 else 0
            return self._ok({
                "items": [r.to_dict() for r in records],
                "total": total, "page": page, "page_size": page_size,
                "total_pages": total_pages,
            })
        except Exception as exc:
            self._logger.error("WebUI list_recycle 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _restore_profile(self, request: web.Request) -> web.Response:
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            record = self._db.restore_profile(record_id)
            if record is None:
                return self._err("记录不存在", 404)
            return self._ok(record.to_dict())
        except Exception as exc:
            self._logger.error("WebUI restore 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _purge_profile(self, request: web.Request) -> web.Response:
        try:
            record_id = self._to_int(request.match_info.get("record_id", ""))
            if not record_id:
                return self._err("缺少 record_id")
            # 先查记录拿图片信息
            record = self._db.get_profile_by_id(record_id)
            ok = self._db.purge_profile(record_id)
            if not ok:
                return self._err("记录不在回收站", 404)
            # 删除关联图片缓存
            if record:
                self._delete_image_file(record, self._image_cache_dir)
            return self._ok({"purged_id": record_id})
        except Exception as exc:
            self._logger.error("WebUI purge 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _empty_recycle(self, request: web.Request) -> web.Response:
        try:
            count = self._db.empty_recycle_bin()
            return self._ok({"purged_count": count})
        except Exception as exc:
            self._logger.error("WebUI empty_recycle 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 生成日志 & 分析 ────────────────────────────────

    async def _list_logs(self, request: web.Request) -> web.Response:
        try:
            q = request.query
            page = max(1, self._to_int(q.get("page", "1"), 1))
            page_size = max(1, min(200, self._to_int(q.get("page_size", "50"), 50)))
            offset = (page - 1) * page_size
            success_raw = q.get("success")
            success = None
            if success_raw is not None and success_raw != "" and success_raw != "all":
                success = self._to_int(success_raw, 1)
            records, total = self._db.list_generation_logs(
                source_group=q.get("source_group", ""),
                mode=q.get("mode", ""),
                success=success,
                date_from=self._to_float(q.get("date_from")),
                date_to=self._to_float(q.get("date_to")),
                offset=offset, limit=page_size,
            )
            total_pages = (total + page_size - 1) // page_size if total > 0 else 0
            return self._ok({
                "items": [r.to_dict() for r in records],
                "total": total, "page": page, "page_size": page_size,
                "total_pages": total_pages,
            })
        except Exception as exc:
            self._logger.error("WebUI list_logs 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _get_analytics(self, request: web.Request) -> web.Response:
        try:
            return self._ok(self._db.generation_stats())
        except Exception as exc:
            self._logger.error("WebUI analytics 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    async def _list_tags(self, request: web.Request) -> web.Response:
        try:
            tags = self._db.list_all_tags()
            return self._ok({
                "tags": [{"name": str(t), "count": int(c)} for t, c in tags],
            })
        except Exception as exc:
            self._logger.error("WebUI list_tags 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 群分析 ────────────────────────────────────────────────────

    async def _get_group_analysis(self, request: web.Request) -> web.Response:
        group_id = request.match_info.get("group_id", "")
        if not group_id:
            return self._err("缺少 group_id")
        record = self._db.get_active_group_analysis(group_id)
        if record:
            return self._ok(record.to_dict())
        return self._ok(None)

    # ─── Prompt ────────────────────────────────────────────────────

    async def _list_prompts(self, request: web.Request) -> web.Response:
        return self._ok(dict(self._prompts))

    async def _get_prompt(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if name not in _ALLOWED_PROMPT_NAMES:
            return self._err("无效的 prompt 名称", 404)
        return self._ok({"name": name, "content": self._prompts.get(name, "")})

    async def _update_prompt(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if name not in _ALLOWED_PROMPT_NAMES:
            return self._err("无效的 prompt 名称", 404)
        try:
            body = await request.json()
            content = str(body.get("content") or "")
            file_path = self._prompts_dir / f"{name}.txt"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            self._prompts[name] = content
            self._logger.info("Prompt 已更新: %s", name)
            return self._ok({"name": name, "content": content})
        except Exception as exc:
            self._logger.error("WebUI update_prompt 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 配置 & 统计 ───────────────────────────────────────────────

    async def _get_config(self, request: web.Request) -> web.Response:
        try:
            cfg_obj = self._plugin.config.portrayal if hasattr(self._plugin.config, 'portrayal') else None
            cfg_dict = {}
            if cfg_obj:
                # Pydantic v2: 用 model_dump；v1/fallback: 遍历非私有属性
                if hasattr(cfg_obj, 'model_dump'):
                    cfg_dict = cfg_obj.model_dump()
                else:
                    for attr in dir(cfg_obj):
                        if not attr.startswith('_') and not callable(getattr(cfg_obj, attr)):
                            try:
                                val = getattr(cfg_obj, attr)
                                if isinstance(val, (str, int, float, bool, list, type(None))):
                                    cfg_dict[attr] = val
                            except Exception:
                                pass
            return self._ok({
                "db_path": str(self._db.db_path),
                "prompts_dir": str(self._prompts_dir),
                "webui_port": self._port,
                "webui_host": self._host,
                "auth_enabled": bool(self._auth_token),
                "config": cfg_dict,
            })
        except Exception as exc:
            self._logger.error("WebUI get_config 失败: %s\n%s", exc, __import__('traceback').format_exc())
            return self._err(str(exc), 500)

    async def _update_config(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            if not hasattr(self._plugin.config, 'portrayal'):
                return self._err("配置对象不可用", 500)
            cfg = self._plugin.config.portrayal
            updated = []
            for key, val in data.items():
                if hasattr(cfg, key):
                    old_val = getattr(cfg, key)
                    # 类型转换
                    if isinstance(old_val, bool):
                        val = str(val).lower() in ('true', '1', 'yes')
                    elif isinstance(old_val, int):
                        val = int(val)
                    elif isinstance(old_val, float):
                        val = float(val)
                    elif isinstance(old_val, list):
                        if isinstance(val, str):
                            val = [v.strip() for v in val.split(',') if v.strip()]
                    setattr(cfg, key, val)
                    updated.append(key)
            # 持久化到 config.toml
            try:
                import tomli_w
                from pathlib import Path
                config_path = Path(self._plugin.ctx.paths.plugin_dir) / "portrayal" / "config.toml"
                config_data = {"plugin": {"config_version": "1.0.0", "enabled": True}, "portrayal": {}}
                for attr in dir(cfg):
                    if not attr.startswith('_') and not callable(getattr(cfg, attr)):
                        config_data["portrayal"][attr] = getattr(cfg, attr)
                with open(config_path, 'wb') as f:
                    tomli_w.dump(config_data, f)
            except Exception:
                pass  # 持久化失败不影响运行时
            self._logger.info("配置已更新: %s", ', '.join(updated))
            return self._ok({"updated": updated})
        except Exception as exc:
            self._logger.error("更新配置失败: %s", exc)
            return self._err(str(exc), 500)

    async def _get_stats(self, request: web.Request) -> web.Response:
        try:
            records = self._db.list_all_active()
            total = len(records)
            locked_count = sum(1 for r in records if r.locked)
            by_mode: dict[str, int] = {}
            by_group: dict[str, int] = {}
            for r in records:
                by_mode[r.mode] = by_mode.get(r.mode, 0) + 1
                g = r.source_group or "未知群"
                by_group[g] = by_group.get(g, 0) + 1
            groups = self._db.list_groups()
            return self._ok({
                "total_active": total,
                "locked": locked_count,
                "by_mode": by_mode,
                "by_group": by_group,
                "group_count": len(groups),
            })
        except Exception as exc:
            self._logger.error("WebUI stats 失败: %s\n%s", exc, traceback.format_exc())
            return self._err(str(exc), 500)

    # ─── 静态文件 ──────────────────────────────────────────────────

    async def _serve_index(self, request: web.Request) -> web.Response:
        index_path = self._dist_dir / "index.html"
        if not index_path.is_file():
            return web.Response(status=404, text="index.html not found")
        data = index_path.read_bytes()
        return web.Response(body=data, content_type="text/html", charset="utf-8",
                             headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    async def _serve_static(self, request: web.Request) -> web.Response:
        tail = request.match_info.get("tail", "")
        # 安全检查：防止路径遍历
        file_path = (self._dist_dir / tail).resolve()
        if not str(file_path).startswith(str(self._dist_dir.resolve())):
            return web.Response(status=404, text="Not Found")

        if not file_path.is_file():
            # fallback 到 index.html（SPA）
            index_path = self._dist_dir / "index.html"
            if index_path.is_file():
                data = index_path.read_bytes()
                return web.Response(body=data, content_type="text/html", charset="utf-8",
                                     headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
            return web.Response(status=404, text="Not Found")

        suffix = file_path.suffix.lower()
        mime = _MIME_MAP.get(suffix, "application/octet-stream")
        data = file_path.read_bytes()
        if mime == "text/html":
            return web.Response(body=data, content_type="text/html", charset="utf-8")
        return web.Response(body=data, content_type=mime)
