"""独立 SQLite 存储层 — 人物画像版本历史 + 群维度管理。

表结构：
  plugin_portrayal_profiles: 画像主表，每次分析一条记录，支持版本历史
  plugin_portrayal_generation_log: 生成日志，记录每次画像触发的元数据与 token 用量
  plugin_portrayal_group_meta: 群号 -> 群名缓存
  v0.4.1 新增: locked, tags 字段
  v0.5.0 新增: user_id, deleted_at 字段 + 生成日志表 + 群名缓存表
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ProfileRecord:
    """一条画像记录。"""
    id: int
    person_id: str
    person_name: str
    stream_id: str
    source_group: str
    profile_text: str
    display_text: str
    mode: str
    msg_count: int
    target_msg_count: int
    version: int
    is_active: int
    created_at: float
    updated_at: float
    locked: int = 0
    tags: str = "[]"
    user_id: str = ""
    deleted_at: float = 0.0
    user_stats: str = ""  # JSON: {top_words:[{word,count}], catchphrases:[{phrase,count}], msg_count:N}

    def to_dict(self) -> dict:
        try:
            tags_list = json.loads(self.tags) if self.tags else []
            if not isinstance(tags_list, list):
                tags_list = []
        except (json.JSONDecodeError, TypeError):
            tags_list = []
        return {
            "id": self.id,
            "person_id": self.person_id,
            "person_name": self.person_name,
            "user_id": self.user_id,
            "stream_id": self.stream_id,
            "source_group": self.source_group,
            "profile_text": self.profile_text,
            "display_text": self.display_text,
            "mode": self.mode,
            "msg_count": self.msg_count,
            "target_msg_count": self.target_msg_count,
            "version": self.version,
            "is_active": bool(self.is_active),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "locked": bool(self.locked),
            "tags": tags_list,
            "deleted_at": self.deleted_at,
            "user_stats": json.loads(self.user_stats) if self.user_stats else None,
        }


@dataclass
class GenerationLogRecord:
    """一条生成日志记录。"""
    id: int
    ts: float
    operator_user_id: str
    operator_name: str
    target_user_id: str
    target_person_id: str
    target_name: str
    stream_id: str
    source_group: str
    mode: str
    scanned_msgs: int
    target_msgs: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int
    success: int
    error: str
    record_id: int
    version: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "operator_user_id": self.operator_user_id,
            "operator_name": self.operator_name,
            "target_user_id": self.target_user_id,
            "target_person_id": self.target_person_id,
            "target_name": self.target_name,
            "stream_id": self.stream_id,
            "source_group": self.source_group,
            "mode": self.mode,
            "scanned_msgs": self.scanned_msgs,
            "target_msgs": self.target_msgs,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
            "success": bool(self.success),
            "error": self.error,
            "record_id": self.record_id,
            "version": self.version,
        }


_SORT_WHITELIST = {"updated_at", "created_at", "version", "person_name", "mode", "msg_count"}


class ProfileDB:
    """画像 SQLite 存储层。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plugin_portrayal_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL,
                    person_name TEXT DEFAULT '',
                    stream_id TEXT DEFAULT '',
                    source_group TEXT DEFAULT '',
                    profile_text TEXT NOT NULL,
                    display_text TEXT DEFAULT '',
                    mode TEXT DEFAULT 'portrait',
                    msg_count INTEGER DEFAULT 0,
                    target_msg_count INTEGER DEFAULT 0,
                    version INTEGER DEFAULT 1,
                    is_active INTEGER DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    locked INTEGER DEFAULT 0,
                    tags TEXT DEFAULT '[]',
                    user_id TEXT DEFAULT '',
                    deleted_at REAL DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_person ON plugin_portrayal_profiles(person_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_stream ON plugin_portrayal_profiles(stream_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_person_stream ON plugin_portrayal_profiles(person_id, stream_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_active ON plugin_portrayal_profiles(is_active, person_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_source_group ON plugin_portrayal_profiles(source_group)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plugin_portrayal_generation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    operator_user_id TEXT DEFAULT '',
                    operator_name TEXT DEFAULT '',
                    target_user_id TEXT DEFAULT '',
                    target_person_id TEXT DEFAULT '',
                    target_name TEXT DEFAULT '',
                    stream_id TEXT DEFAULT '',
                    source_group TEXT DEFAULT '',
                    mode TEXT DEFAULT 'portrait',
                    scanned_msgs INTEGER DEFAULT 0,
                    target_msgs INTEGER DEFAULT 0,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    duration_ms INTEGER DEFAULT 0,
                    success INTEGER DEFAULT 1,
                    error TEXT DEFAULT '',
                    record_id INTEGER DEFAULT 0,
                    version INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gl_ts ON plugin_portrayal_generation_log(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gl_group ON plugin_portrayal_generation_log(source_group)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plugin_portrayal_group_meta (
                    group_id TEXT PRIMARY KEY,
                    group_name TEXT DEFAULT '',
                    member_count INTEGER DEFAULT 0,
                    updated_at REAL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plugin_portrayal_group_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_group TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    hot_topics TEXT DEFAULT '[]',
                    top_words TEXT DEFAULT '[]',
                    user_catchphrases TEXT DEFAULT '{}',
                    user_relations TEXT DEFAULT '{}',
                    summary TEXT DEFAULT '',
                    message_count INTEGER DEFAULT 0,
                    user_count INTEGER DEFAULT 0,
                    version INTEGER DEFAULT 1,
                    is_active INTEGER DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    deleted_at REAL DEFAULT 0
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self) -> None:
        """兼容旧表：检测并添加新字段。"""
        conn = self._conn()
        try:
            cursor = conn.execute("PRAGMA table_info(plugin_portrayal_profiles)")
            existing = {row[1] for row in cursor.fetchall()}
            if "locked" not in existing:
                conn.execute("ALTER TABLE plugin_portrayal_profiles ADD COLUMN locked INTEGER DEFAULT 0")
            if "tags" not in existing:
                conn.execute("ALTER TABLE plugin_portrayal_profiles ADD COLUMN tags TEXT DEFAULT '[]'")
            if "user_id" not in existing:
                conn.execute("ALTER TABLE plugin_portrayal_profiles ADD COLUMN user_id TEXT DEFAULT ''")
            if "deleted_at" not in existing:
                conn.execute("ALTER TABLE plugin_portrayal_profiles ADD COLUMN deleted_at REAL DEFAULT 0")
            if "user_stats" not in existing:
                conn.execute("ALTER TABLE plugin_portrayal_profiles ADD COLUMN user_stats TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_source_group ON plugin_portrayal_profiles(source_group)")
            conn.commit()
        finally:
            conn.close()

    # ─── 写入 ──────────────────────────────────────────────────────

    def save_profile(
        self,
        *,
        person_id: str,
        person_name: str,
        stream_id: str,
        source_group: str,
        profile_text: str,
        display_text: str,
        mode: str,
        msg_count: int,
        target_msg_count: int,
        user_id: str = "",
        user_stats: str = "",
    ) -> ProfileRecord:
        """保存一条新画像，同时将同 person_id + stream_id 的旧记录标记为 inactive。
        锁定的旧记录不会被覆盖。"""

        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 停用旧活跃记录（跳过锁定的）
            conn.execute(
                "UPDATE plugin_portrayal_profiles SET is_active = 0 "
                "WHERE person_id = ? AND stream_id = ? AND is_active = 1 AND locked = 0",
                (person_id, stream_id),
            )

            # 检查是否有锁定的活跃记录，有的话新记录不激活
            cursor = conn.execute(
                "SELECT COUNT(*) FROM plugin_portrayal_profiles "
                "WHERE person_id = ? AND stream_id = ? AND is_active = 1 AND locked = 1",
                (person_id, stream_id),
            )
            has_locked_active = cursor.fetchone()[0] > 0

            cursor = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM plugin_portrayal_profiles WHERE person_id = ? AND stream_id = ?",
                (person_id, stream_id),
            )
            version = cursor.fetchone()[0]

            new_is_active = 0 if has_locked_active else 1

            cursor = conn.execute(
                """
                INSERT INTO plugin_portrayal_profiles (
                    person_id, person_name, stream_id, source_group,
                    profile_text, display_text, mode,
                    msg_count, target_msg_count, version,
                    is_active, created_at, updated_at, locked, tags, user_id, deleted_at, user_stats
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', ?, 0, ?)
                """,
                (person_id, person_name, stream_id, source_group,
                 profile_text, display_text, mode,
                 msg_count, target_msg_count, version,
                 new_is_active, now, now, user_id, user_stats),
            )
            record_id = cursor.lastrowid
            conn.commit()
            return self.get_profile_by_id(record_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ─── 查询 ──────────────────────────────────────────────────────

    def get_profile_by_id(self, record_id: int) -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM plugin_portrayal_profiles WHERE id = ?",
                (record_id,),
            )
            row = cursor.fetchone()
            return ProfileRecord(**dict(row)) if row else None
        finally:
            conn.close()

    def get_active_profile(self, person_id: str, stream_id: str = "") -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            if stream_id:
                cursor = conn.execute(
                    "SELECT * FROM plugin_portrayal_profiles WHERE person_id = ? AND stream_id = ? AND is_active = 1 AND deleted_at = 0 ORDER BY updated_at DESC LIMIT 1",
                    (person_id, stream_id),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM plugin_portrayal_profiles WHERE person_id = ? AND is_active = 1 AND deleted_at = 0 ORDER BY updated_at DESC LIMIT 1",
                    (person_id,),
                )
            row = cursor.fetchone()
            return ProfileRecord(**dict(row)) if row else None
        finally:
            conn.close()

    def get_active_profile_by_stream(self, stream_id: str) -> list[ProfileRecord]:
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM plugin_portrayal_profiles WHERE stream_id = ? AND is_active = 1 AND deleted_at = 0 ORDER BY updated_at DESC",
                (stream_id,),
            )
            return [ProfileRecord(**dict(row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_active_profile_by_group(self, person_id: str, source_group: str) -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM plugin_portrayal_profiles WHERE person_id = ? AND source_group = ? AND is_active = 1 AND deleted_at = 0 ORDER BY updated_at DESC LIMIT 1",
                (person_id, source_group),
            )
            row = cursor.fetchone()
            return ProfileRecord(**dict(row)) if row else None
        finally:
            conn.close()

    def get_history_by_group(self, person_id: str, source_group: str) -> list[ProfileRecord]:
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM plugin_portrayal_profiles WHERE person_id = ? AND source_group = ? AND deleted_at = 0 ORDER BY version DESC",
                (person_id, source_group),
            )
            return [ProfileRecord(**dict(row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def list_all_active(self) -> list[ProfileRecord]:
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM plugin_portrayal_profiles WHERE is_active = 1 AND deleted_at = 0 ORDER BY updated_at DESC"
            )
            return [ProfileRecord(**dict(row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_all_active_for_person(self, person_id: str) -> list[ProfileRecord]:
        """查同一 person_id 在所有群下的活跃画像（含当前群）。"""
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM plugin_portrayal_profiles WHERE person_id = ? AND is_active = 1 AND deleted_at = 0 ORDER BY updated_at DESC",
                (person_id,),
            )
            return [ProfileRecord(**dict(row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def search_users(self, query: str, limit: int = 20) -> list[dict]:
        """在画像库中搜索用户（去重），返回用户级信息。"""
        conn = self._conn()
        try:
            like = f"%{query}%"
            cursor = conn.execute(
                """SELECT DISTINCT person_id, person_name, user_id,
                          MAX(updated_at) as latest_updated, COUNT(*) as version_count
                   FROM plugin_portrayal_profiles
                   WHERE deleted_at = 0
                     AND (person_name LIKE ? OR user_id LIKE ? OR person_id LIKE ?)
                   GROUP BY person_id
                   ORDER BY latest_updated DESC
                   LIMIT ?""",
                (like, like, like, limit),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_user_group_profiles(self, person_id: str) -> list[dict]:
        """查同一 person_id 在各群的画像状态（活跃版本数、最新版本号、更新时间）。"""
        conn = self._conn()
        try:
            cursor = conn.execute(
                """SELECT stream_id, source_group, MAX(version) as max_version,
                          COUNT(*) as version_count, MAX(updated_at) as latest_updated,
                          MAX(is_active) as has_active
                   FROM plugin_portrayal_profiles
                   WHERE person_id = ? AND deleted_at = 0
                   GROUP BY stream_id
                   ORDER BY latest_updated DESC""",
                (person_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_history(self, person_id: str, stream_id: str = "") -> list[ProfileRecord]:
        conn = self._conn()
        try:
            if stream_id:
                cursor = conn.execute(
                    "SELECT * FROM plugin_portrayal_profiles WHERE person_id = ? AND stream_id = ? AND deleted_at = 0 ORDER BY version DESC",
                    (person_id, stream_id),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM plugin_portrayal_profiles WHERE person_id = ? AND deleted_at = 0 ORDER BY version DESC",
                    (person_id,),
                )
            return [ProfileRecord(**dict(row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def list_profiles(
        self,
        *,
        search: str = "",
        source_group: str = "",
        mode: str = "",
        tags: list[str] | None = None,
        locked: int | None = None,
        is_active: int | None = 1,
        date_from: float | None = None,
        date_to: float | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        offset: int = 0,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> tuple[list[ProfileRecord], int]:
        """动态构建 WHERE 子句，返回 (记录列表, 总数)。"""

        where_parts: list[str] = []
        params: list = []

        if include_deleted:
            where_parts.append("deleted_at > 0")
        else:
            where_parts.append("deleted_at = 0")

        if search:
            where_parts.append("(person_name LIKE ? OR person_id LIKE ? OR source_group LIKE ? OR user_id LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like])

        if source_group:
            where_parts.append("source_group = ?")
            params.append(source_group)

        if mode:
            where_parts.append("mode = ?")
            params.append(mode)

        if tags:
            for tag in tags:
                where_parts.append("tags LIKE ?")
                params.append(f'%"{tag}"%')

        if locked is not None:
            where_parts.append("locked = ?")
            params.append(locked)

        if is_active is not None:
            where_parts.append("is_active = ?")
            params.append(is_active)

        if date_from is not None:
            where_parts.append("updated_at >= ?")
            params.append(date_from)

        if date_to is not None:
            where_parts.append("updated_at <= ?")
            params.append(date_to)

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"

        # 排序白名单防注入
        safe_sort = sort_by if sort_by in _SORT_WHITELIST else "updated_at"
        safe_order = "ASC" if sort_order.lower() == "asc" else "DESC"

        conn = self._conn()
        try:
            count_cursor = conn.execute(
                f"SELECT COUNT(*) FROM plugin_portrayal_profiles WHERE {where_clause}",
                params,
            )
            total = count_cursor.fetchone()[0]

            cursor = conn.execute(
                f"SELECT * FROM plugin_portrayal_profiles WHERE {where_clause} "
                f"ORDER BY {safe_sort} {safe_order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            records = [ProfileRecord(**dict(row)) for row in cursor.fetchall()]
            return records, total
        finally:
            conn.close()

    def list_groups(self) -> list[str]:
        """返回所有去重的 source_group。"""
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT DISTINCT source_group FROM plugin_portrayal_profiles "
                "WHERE source_group != '' AND deleted_at = 0 ORDER BY source_group"
            )
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    def list_all_tags(self) -> list[tuple[str, int]]:
        """返回所有标签及其出现次数。"""
        conn = self._conn()
        try:
            cursor = conn.execute(
                "SELECT tags FROM plugin_portrayal_profiles WHERE tags != '[]' AND tags != '' AND deleted_at = 0"
            )
            tag_counts: dict[str, int] = {}
            for row in cursor.fetchall():
                try:
                    tags_list = json.loads(row[0])
                    if isinstance(tags_list, list):
                        for tag in tags_list:
                            tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1
                except (json.JSONDecodeError, TypeError):
                    continue
            return sorted(tag_counts.items(), key=lambda x: -x[1])
        finally:
            conn.close()

    # ─── 状态变更 ──────────────────────────────────────────────────

    def deactivate(self, person_id: str, stream_id: str = "") -> int:
        conn = self._conn()
        try:
            if stream_id:
                cursor = conn.execute(
                    "UPDATE plugin_portrayal_profiles SET is_active = 0 WHERE person_id = ? AND stream_id = ? AND is_active = 1",
                    (person_id, stream_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE plugin_portrayal_profiles SET is_active = 0 WHERE person_id = ? AND is_active = 1",
                    (person_id,),
                )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def activate_version(self, record_id: int) -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute("SELECT * FROM plugin_portrayal_profiles WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return None
            record = ProfileRecord(**dict(row))

            conn.execute(
                "UPDATE plugin_portrayal_profiles SET is_active = 0 WHERE person_id = ? AND stream_id = ? AND is_active = 1",
                (record.person_id, record.stream_id),
            )
            conn.execute(
                "UPDATE plugin_portrayal_profiles SET is_active = 1, updated_at = ? WHERE id = ?",
                (time.time(), record_id),
            )
            conn.commit()
            return self.get_profile_by_id(record_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ─── 编辑 ──────────────────────────────────────────────────────

    def update_profile_text(self, record_id: int, profile_text: str) -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE plugin_portrayal_profiles SET profile_text = ?, updated_at = ? WHERE id = ?",
                (profile_text, time.time(), record_id),
            )
            conn.commit()
            return self.get_profile_by_id(record_id)
        finally:
            conn.close()

    def update_display_text(self, record_id: int, display_text: str) -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE plugin_portrayal_profiles SET display_text = ?, updated_at = ? WHERE id = ?",
                (display_text, time.time(), record_id),
            )
            conn.commit()
            return self.get_profile_by_id(record_id)
        finally:
            conn.close()

    def update_tags(self, record_id: int, tags: list[str]) -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE plugin_portrayal_profiles SET tags = ?, updated_at = ? WHERE id = ?",
                (json.dumps(tags, ensure_ascii=False), time.time(), record_id),
            )
            conn.commit()
            return self.get_profile_by_id(record_id)
        finally:
            conn.close()

    def set_locked(self, record_id: int, locked: bool) -> Optional[ProfileRecord]:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE plugin_portrayal_profiles SET locked = ? WHERE id = ?",
                (1 if locked else 0, record_id),
            )
            conn.commit()
            return self.get_profile_by_id(record_id)
        finally:
            conn.close()

    # ─── 删除 ──────────────────────────────────────────────────────

    # ─── 删除（软删除 + 回收站）─────────────────────────────

    def delete_profile(self, record_id: int) -> bool:
        """软删除：打 deleted_at 标记，同时停用。数据进回收站，可恢复。"""
        conn = self._conn()
        try:
            cursor = conn.execute(
                "UPDATE plugin_portrayal_profiles SET deleted_at = ?, is_active = 0 WHERE id = ? AND deleted_at = 0",
                (time.time(), record_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def restore_profile(self, record_id: int) -> Optional[ProfileRecord]:
        """从回收站恢复（不自动重新激活，避免与现有活跃版本冲突）。"""
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE plugin_portrayal_profiles SET deleted_at = 0 WHERE id = ?",
                (record_id,),
            )
            conn.commit()
            return self.get_profile_by_id(record_id)
        finally:
            conn.close()

    def purge_profile(self, record_id: int) -> bool:
        """从回收站彻底物理删除。"""
        conn = self._conn()
        try:
            cursor = conn.execute(
                "DELETE FROM plugin_portrayal_profiles WHERE id = ? AND deleted_at > 0",
                (record_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def empty_recycle_bin(self) -> int:
        """清空回收站（物理删除所有已软删记录）。"""
        conn = self._conn()
        try:
            cursor = conn.execute("DELETE FROM plugin_portrayal_profiles WHERE deleted_at > 0")
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def delete_person_profiles(self, person_id: str, stream_id: str = "") -> int:
        """软删除该用户（该群）的全部画像。"""
        now = time.time()
        conn = self._conn()
        try:
            if stream_id:
                cursor = conn.execute(
                    "UPDATE plugin_portrayal_profiles SET deleted_at = ?, is_active = 0 WHERE person_id = ? AND stream_id = ? AND deleted_at = 0",
                    (now, person_id, stream_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE plugin_portrayal_profiles SET deleted_at = ?, is_active = 0 WHERE person_id = ? AND deleted_at = 0",
                    (now, person_id),
                )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    # ─── 生成日志 ───────────────────────────────────────

    def add_generation_log(
        self,
        *,
        operator_user_id: str = "",
        operator_name: str = "",
        target_user_id: str = "",
        target_person_id: str = "",
        target_name: str = "",
        stream_id: str = "",
        source_group: str = "",
        mode: str = "portrait",
        scanned_msgs: int = 0,
        target_msgs: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        duration_ms: int = 0,
        success: bool = True,
        error: str = "",
        record_id: int = 0,
        version: int = 0,
    ) -> int:
        conn = self._conn()
        try:
            cursor = conn.execute(
                """
                INSERT INTO plugin_portrayal_generation_log (
                    ts, operator_user_id, operator_name, target_user_id,
                    target_person_id, target_name, stream_id, source_group,
                    mode, scanned_msgs, target_msgs, prompt_tokens,
                    completion_tokens, total_tokens, duration_ms, success,
                    error, record_id, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), operator_user_id, operator_name, target_user_id,
                 target_person_id, target_name, stream_id, source_group,
                 mode, scanned_msgs, target_msgs, prompt_tokens,
                 completion_tokens, total_tokens, duration_ms, 1 if success else 0,
                 error, record_id, version),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def list_generation_logs(
        self,
        *,
        source_group: str = "",
        mode: str = "",
        success: int | None = None,
        date_from: float | None = None,
        date_to: float | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[GenerationLogRecord], int]:
        where_parts: list[str] = []
        params: list = []
        if source_group:
            where_parts.append("source_group = ?")
            params.append(source_group)
        if mode:
            where_parts.append("mode = ?")
            params.append(mode)
        if success is not None:
            where_parts.append("success = ?")
            params.append(success)
        if date_from is not None:
            where_parts.append("ts >= ?")
            params.append(date_from)
        if date_to is not None:
            where_parts.append("ts <= ?")
            params.append(date_to)
        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        conn = self._conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM plugin_portrayal_generation_log WHERE {where_clause}",
                params,
            ).fetchone()[0]
            cursor = conn.execute(
                f"SELECT * FROM plugin_portrayal_generation_log WHERE {where_clause} "
                f"ORDER BY ts DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            records = [GenerationLogRecord(**dict(row)) for row in cursor.fetchall()]
            return records, total
        finally:
            conn.close()

    def generation_stats(self) -> dict:
        """聚合生成日志：总次数、成功率、token 总量、按群/按模式分布。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(success) AS ok, "
                "SUM(total_tokens) AS tok, "
                "SUM(prompt_tokens) AS ptok, "
                "SUM(completion_tokens) AS ctok, "
                "AVG(duration_ms) AS dur "
                "FROM plugin_portrayal_generation_log"
            ).fetchone()
            total = row["n"] or 0
            ok = row["ok"] or 0
            by_group_rows = conn.execute(
                "SELECT source_group, COUNT(*) AS n, SUM(total_tokens) AS tok "
                "FROM plugin_portrayal_generation_log GROUP BY source_group ORDER BY tok DESC"
            ).fetchall()
            by_mode_rows = conn.execute(
                "SELECT mode, COUNT(*) AS n, SUM(total_tokens) AS tok "
                "FROM plugin_portrayal_generation_log GROUP BY mode"
            ).fetchall()
            return {
                "total_runs": total,
                "success_runs": ok,
                "fail_runs": total - ok,
                "total_tokens": row["tok"] or 0,
                "prompt_tokens": row["ptok"] or 0,
                "completion_tokens": row["ctok"] or 0,
                "avg_duration_ms": int(row["dur"] or 0),
                "by_group": [
                    {"group": r["source_group"] or "", "runs": r["n"], "tokens": r["tok"] or 0}
                    for r in by_group_rows
                ],
                "by_mode": [
                    {"mode": r["mode"] or "", "runs": r["n"], "tokens": r["tok"] or 0}
                    for r in by_mode_rows
                ],
            }
        finally:
            conn.close()

    # ─── 群名缓存 ───────────────────────────────────────

    def upsert_group_meta(self, group_id: str, group_name: str, member_count: int = 0) -> None:
        if not group_id:
            return
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO plugin_portrayal_group_meta (group_id, group_name, member_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    group_name = excluded.group_name,
                    member_count = excluded.member_count,
                    updated_at = excluded.updated_at
                """,
                (str(group_id), group_name, member_count, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_group_meta(self, group_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM plugin_portrayal_group_meta WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_group_meta(self) -> dict[str, dict]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM plugin_portrayal_group_meta").fetchall()
            return {str(r["group_id"]): dict(r) for r in rows}
        finally:
            conn.close()

    # ─── 工具 ──────────────────────────────────────────────────────

    def is_profile_expired(self, record: ProfileRecord, ttl_days: int) -> bool:
        if ttl_days <= 0:
            return False
        elapsed = time.time() - record.updated_at
        return elapsed > ttl_days * 86400


# ═══════════════════════════════════════════════════════════════════════
# GroupAnalysis — 群分析存储
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class GroupAnalysisRecord:
    id: int
    source_group: str
    tags: str = "[]"
    hot_topics: str = "[]"
    top_words: str = "[]"
    user_catchphrases: str = "{}"
    user_relations: str = "{}"
    summary: str = ""
    message_count: int = 0
    user_count: int = 0
    version: int = 1
    is_active: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    deleted_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_group": self.source_group,
            "tags": json.loads(self.tags) if self.tags else [],
            "hot_topics": json.loads(self.hot_topics) if self.hot_topics else [],
            "top_words": json.loads(self.top_words) if self.top_words else [],
            "user_catchphrases": json.loads(self.user_catchphrases) if self.user_catchphrases else {},
            "user_relations": json.loads(self.user_relations) if self.user_relations else {},
            "summary": self.summary,
            "message_count": self.message_count,
            "user_count": self.user_count,
            "version": self.version,
            "is_active": bool(self.is_active),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ─── 群分析操作 ───────────────────────────────────────────────

def save_group_analysis(
    self,
    *,
    source_group: str,
    tags: list[str],
    hot_topics: list[dict],
    top_words: list[dict],
    user_catchphrases: dict,
    user_relations: dict,
    summary: str,
    message_count: int,
    user_count: int,
) -> GroupAnalysisRecord:
    conn = self._conn()
    now = time.time()
    try:
        # 新版本号
        existing = conn.execute(
            "SELECT MAX(version) as mv FROM plugin_portrayal_group_analysis "
            "WHERE source_group = ? AND deleted_at = 0",
            (source_group,),
        ).fetchone()
        version = (existing["mv"] or 0) + 1 if existing else 1

        # 旧版标记 inactive
        conn.execute(
            "UPDATE plugin_portrayal_group_analysis SET is_active = 0 WHERE source_group = ? AND is_active = 1",
            (source_group,),
        )

        cursor = conn.execute(
            """INSERT INTO plugin_portrayal_group_analysis
               (source_group, tags, hot_topics, top_words, user_catchphrases,
                user_relations, summary, message_count, user_count,
                version, is_active, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (
                source_group,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(hot_topics, ensure_ascii=False),
                json.dumps(top_words, ensure_ascii=False),
                json.dumps(user_catchphrases, ensure_ascii=False),
                json.dumps(user_relations, ensure_ascii=False),
                summary,
                message_count,
                user_count,
                version,
                now,
                now,
            ),
        )
        conn.commit()
        return GroupAnalysisRecord(
            id=cursor.lastrowid,
            source_group=source_group,
            tags=json.dumps(tags, ensure_ascii=False),
            hot_topics=json.dumps(hot_topics, ensure_ascii=False),
            top_words=json.dumps(top_words, ensure_ascii=False),
            user_catchphrases=json.dumps(user_catchphrases, ensure_ascii=False),
            user_relations=json.dumps(user_relations, ensure_ascii=False),
            summary=summary,
            message_count=message_count,
            user_count=user_count,
            version=version,
            is_active=1,
            created_at=now,
            updated_at=now,
        )
    finally:
        conn.close()

def get_active_group_analysis(self, source_group: str) -> Optional[GroupAnalysisRecord]:
    conn = self._conn()
    try:
        row = conn.execute(
            "SELECT * FROM plugin_portrayal_group_analysis "
            "WHERE source_group = ? AND is_active = 1 AND deleted_at = 0 "
            "ORDER BY version DESC LIMIT 1",
            (source_group,),
        ).fetchone()
        return GroupAnalysisRecord(**dict(row)) if row else None
    finally:
        conn.close()

def get_group_analysis_history(self, source_group: str) -> list[GroupAnalysisRecord]:
    conn = self._conn()
    try:
        rows = conn.execute(
            "SELECT * FROM plugin_portrayal_group_analysis "
            "WHERE source_group = ? AND deleted_at = 0 ORDER BY version DESC",
            (source_group,),
        ).fetchall()
        return [GroupAnalysisRecord(**dict(r)) for r in rows]
    finally:
        conn.close()
