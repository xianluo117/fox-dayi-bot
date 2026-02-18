from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

def is_admin(interaction: discord.Interaction) -> bool:  # type: ignore
    """RoleConfigure 的管理员鉴权
    """

    # 使用本项目的管理员列表（bot.admins）
    try:
        admins = getattr(interaction.client, "admins", [])
        if interaction.user is not None and int(interaction.user.id) in [int(x) for x in admins]:
            return True
    except Exception:
        pass

    return False

# 北京时间/中国标准时间
try:
    from zoneinfo import ZoneInfo

    BJ_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    BJ_TZ = timezone(timedelta(hours=8))


LOG = logging.getLogger(__name__)


class ConfirmActionView(discord.ui.View):
    """通用二次确认 View。"""

    def __init__(self, author_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.author_id = int(author_id)
        self.confirmed: bool = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user is not None and int(interaction.user.id) == self.author_id

    @discord.ui.button(label="确认", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        await safe_defer(interaction)
        self.confirmed = True
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore
        try:
            await interaction.followup.send("已确认，正在执行…", ephemeral=True)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        await safe_defer(interaction)
        self.confirmed = False
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore
        try:
            await interaction.followup.send("已取消。", ephemeral=True)
        except Exception:
            pass
        self.stop()


ROLE_CONFIGURE_DIR = Path("role_configure")
AVAILABLE_CHANNEL_PATH = ROLE_CONFIGURE_DIR / "available_channel.json"
PANELS_PATH = ROLE_CONFIGURE_DIR / "panels.json"
TIMED_ROLE_DB_PATH = ROLE_CONFIGURE_DIR / "timed_role_members.db"


async def safe_defer(interaction: discord.Interaction) -> None:
    """避免 Unknown interaction / 重复 response。"""

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


def _ensure_role_configure_dir() -> None:
    ROLE_CONFIGURE_DIR.mkdir(parents=True, exist_ok=True)


def load_available_channels() -> set[int]:
    """从 available_channel.json 读取监听频道 ID 列表。"""

    _ensure_role_configure_dir()
    if not AVAILABLE_CHANNEL_PATH.exists():
        AVAILABLE_CHANNEL_PATH.write_text("[]", encoding="utf-8")
        return set()

    try:
        data = json.loads(AVAILABLE_CHANNEL_PATH.read_text(encoding="utf-8") or "[]")
        if not isinstance(data, list):
            return set()
        return {int(x) for x in data}
    except Exception:
        LOG.exception("Failed to load %s", AVAILABLE_CHANNEL_PATH)
        return set()


def load_panels() -> dict[str, dict[str, Any]]:
    _ensure_role_configure_dir()
    if not PANELS_PATH.exists():
        PANELS_PATH.write_text("{}", encoding="utf-8")
        return {}

    try:
        raw = PANELS_PATH.read_text(encoding="utf-8")
        raw = raw.strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        LOG.exception("Failed to load %s", PANELS_PATH)
        return {}


def save_panels(panels: dict[str, dict[str, Any]]) -> None:
    _ensure_role_configure_dir()
    PANELS_PATH.write_text(json.dumps(panels, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_panel_json(payload: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[str]]:
    """解析/校验面板 JSON（与 role_configure/example.json 一致）。

    必填字段：
    - stats_channel_id: int
    - required_role_id: int
    - check_period_days: int
    - min_messages: int
    - min_mentions: int
    - role_to_grant: int
    - role_to_remove: int
    - duration_days: int
    - custom_title: str
    - custom_desc: str
    - custom_button_text: str
    - reason: str
    """

    problems: list[str] = []

    required_keys = [
        "stats_channel_id",
        "required_role_id",
        "check_period_days",
        "min_messages",
        "min_mentions",
        "role_to_grant",
        "role_to_remove",
        "duration_days",
        "custom_title",
        "custom_desc",
        "custom_button_text",
        "reason",
    ]
    missing = [k for k in required_keys if k not in payload]
    if missing:
        problems.append(f"缺少字段：{', '.join(missing)}")
        return None, problems

    def _as_int(key: str) -> Optional[int]:
        try:
            return int(payload[key])
        except Exception:
            problems.append(f"字段 {key} 必须是整数")
            return None

    def _as_str(key: str) -> Optional[str]:
        try:
            v = payload[key]
            if v is None:
                raise ValueError
            return str(v)
        except Exception:
            problems.append(f"字段 {key} 必须是字符串")
            return None

    stats_channel_id = _as_int("stats_channel_id")
    required_role_id = _as_int("required_role_id")
    check_period_days = _as_int("check_period_days")
    min_messages = _as_int("min_messages")
    min_mentions = _as_int("min_mentions")
    role_to_grant = _as_int("role_to_grant")
    role_to_remove = _as_int("role_to_remove")
    duration_days = _as_int("duration_days")
    custom_title = _as_str("custom_title")
    custom_desc = _as_str("custom_desc")
    custom_button_text = _as_str("custom_button_text")
    reason = _as_str("reason")

    if problems:
        return None, problems

    assert stats_channel_id is not None
    assert required_role_id is not None
    assert check_period_days is not None
    assert min_messages is not None
    assert min_mentions is not None
    assert role_to_grant is not None
    assert role_to_remove is not None
    assert duration_days is not None
    assert custom_title is not None
    assert custom_desc is not None
    assert custom_button_text is not None
    assert reason is not None

    if check_period_days <= 0:
        problems.append("check_period_days 必须 > 0")

    # 复用原有阈值校验规则：允许 0，但不能同时为 0；duration_days 必须 > 0
    # 注意：duration_days 在这里也再次校验。
    if min_messages < 0:
        problems.append("min_messages 不能为负数")
    if min_mentions < 0:
        problems.append("min_mentions 不能为负数")
    if min_messages == 0 and min_mentions == 0:
        problems.append("min_messages 与 min_mentions 不能同时为 0（至少设置一项门槛）")
    if duration_days <= 0:
        problems.append("duration_days 必须 > 0")

    if not custom_title.strip():
        problems.append("custom_title 不能为空")
    if not custom_button_text.strip():
        problems.append("custom_button_text 不能为空")
    if not reason.strip():
        problems.append("reason 不能为空")

    if problems:
        return None, problems

    return {
        "stats_channel_id": stats_channel_id,
        "required_role_id": required_role_id,
        "period_days": check_period_days,
        "required_msg_count": min_messages,
        "required_mention_count": min_mentions,
        "grant_role_id": role_to_grant,
        "remove_role_id": role_to_remove,
        "duration_days": duration_days,
        "custom_title": custom_title,
        "custom_desc": custom_desc,
        "custom_button_text": custom_button_text,
        "reason": reason,
    }, []


def get_bj_date_str(dt_utc: datetime) -> str:
    """将 UTC datetime 转为北京时间日期字符串 YYYYMMDD。"""

    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)

    bj_dt = dt_utc.astimezone(BJ_TZ)
    return bj_dt.strftime("%Y%m%d")


def _channel_db_path(channel_id: int) -> Path:
    return ROLE_CONFIGURE_DIR / f"{channel_id}.db"


def ensure_channel_db(channel_id: int) -> None:
    """确保频道统计库存在，并含所需表/索引。"""

    _ensure_role_configure_dir()
    db_path = _channel_db_path(channel_id)

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_user_stats (
              user_id TEXT NOT NULL,
              date TEXT NOT NULL,
              msg_count INTEGER NOT NULL,
              mention_count INTEGER NOT NULL,
              PRIMARY KEY (user_id, date)
            )
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_daily_user_stats_date
            ON daily_user_stats(date)
            """
        )

        # meta 用于增量更新命令
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


def ensure_timed_role_db() -> None:
    """确保 timed_role_members.db 存在并包含 duration_days 列（兼容旧库迁移）。"""

    _ensure_role_configure_dir()
    conn = sqlite3.connect(str(TIMED_ROLE_DB_PATH))
    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS timed_role_members (
              user_id TEXT NOT NULL,
              role_id TEXT NOT NULL,
              expire_ts INTEGER NOT NULL,
              restore_role_id TEXT,
              duration_days INTEGER NOT NULL,
              PRIMARY KEY (user_id, role_id)
            )
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timed_role_members_expire_ts
            ON timed_role_members(expire_ts)
            """
        )

        # 兼容旧库：缺 duration_days 则补列
        cur.execute("PRAGMA table_info(timed_role_members)")
        cols = {row[1] for row in cur.fetchall()}
        if "duration_days" not in cols:
            cur.execute(
                "ALTER TABLE timed_role_members ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 0"
            )

        conn.commit()
    finally:
        conn.close()


def _db_sum_stats(channel_id: int, user_id: int, since_date: str, until_date: str) -> tuple[int, int]:
    """从频道 DB 汇总 [since_date, until_date] 的统计。阻塞函数。"""

    ensure_channel_db(channel_id)
    conn = sqlite3.connect(str(_channel_db_path(channel_id)))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(msg_count), 0), COALESCE(SUM(mention_count), 0)
            FROM daily_user_stats
            WHERE user_id = ? AND date >= ? AND date <= ?
            """,
            (str(user_id), since_date, until_date),
        )
        row = cur.fetchone()
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)
    finally:
        conn.close()


def _date_range_yyyymmdd(period_days: int, now_utc: Optional[datetime] = None) -> tuple[str, str]:
    """按北京时间计算最近 period_days 天（含今天）的起止日期字符串。"""

    if period_days <= 0:
        raise ValueError("period_days must be positive")
    now_utc = now_utc or datetime.now(timezone.utc)
    now_bj = now_utc.astimezone(BJ_TZ)
    until_date = now_bj.strftime("%Y%m%d")
    since_date = (now_bj - timedelta(days=period_days - 1)).strftime("%Y%m%d")
    return since_date, until_date


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


@dataclass
class PanelConfig:
    uuid: str
    guild_id: int
    panel_channel_id: int
    panel_message_id: int
    required_role_id: int
    stats_channel_id: int
    period_days: int
    required_msg_count: int
    required_mention_count: int
    grant_role_id: int
    remove_role_id: int
    duration_days: int
    reason: str

    @classmethod
    def from_dict(cls, uuid_: str, data: dict[str, Any]) -> "PanelConfig":
        return cls(
            uuid=uuid_,
            guild_id=int(data["guild_id"]),
            panel_channel_id=int(data["panel_channel_id"]),
            panel_message_id=int(data["panel_message_id"]),
            required_role_id=int(data["required_role_id"]),
            stats_channel_id=int(data["stats_channel_id"]),
            period_days=int(data["period_days"]),
            required_msg_count=int(data["required_msg_count"]),
            required_mention_count=int(data["required_mention_count"]),
            grant_role_id=int(data["grant_role_id"]),
            remove_role_id=int(data["remove_role_id"]),
            duration_days=int(data["duration_days"]),
            reason=str(data.get("reason") or ""),
        )


class RoleConfigure(commands.Cog):
    """RoleConfigure：统计发言/提及，并提供限时身份组审核面板。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.available_channels: set[int] = load_available_channels()
        self.panels: dict[str, dict[str, Any]] = load_panels()

        # key: (channel_id, user_id, date_yyyymmdd)
        self.buffer: dict[tuple[int, int, str], dict[str, int]] = {}

        # 临时“测试频道监听”任务：key = (requester_user_id, channel_id)
        # value = asyncio.Task
        self._test_listen_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}

        ensure_timed_role_db()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.abc.GuildChannel):
            return

        channel_id = message.channel.id
        if channel_id not in self.available_channels:
            return

        # 统计：普通消息 + 回复消息（reply）
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return

        date_str = get_bj_date_str(message.created_at)
        key = (channel_id, message.author.id, date_str)

        entry = self.buffer.setdefault(key, {"msg": 0, "mention": 0})
        entry["msg"] += 1

        if message.mentions or message.role_mentions:
            entry["mention"] += 1

        # “测试频道监听”任务（仅用于 DM 汇总，不写入 DB）
        try:
            await self._test_channel_listen_on_message(message)
        except Exception:
            LOG.exception("test channel listen on_message failed")

    async def _test_channel_listen_on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if not isinstance(message.channel, discord.abc.GuildChannel):
            return

        channel_id = int(message.channel.id)
        if not getattr(self, "_test_listen_tasks", None):
            return

        # 监听器需要 per-task 状态，因此状态放在 task 对象上（闭包变量）。
        # 这里通过 message.channel_id 扫描匹配的任务，调用其注入函数。
        for (_req_uid, ch_id), task in list(self._test_listen_tasks.items()):
            if ch_id != channel_id:
                continue
            inject = getattr(task, "_rc_inject_message", None)
            if inject is None:
                continue
            try:
                await inject(message)
            except Exception:
                LOG.exception("inject message failed")

    def _flush_buffer_blocking(self, items: list[tuple[tuple[int, int, str], dict[str, int]]]) -> None:
        """在 executor 中运行的 flush（sqlite3 阻塞 IO）。"""

        # 按 channel 聚合，减少 connect 次数
        by_channel: dict[int, list[tuple[int, str, int, int]]] = {}
        for (channel_id, user_id, date_str), stats in items:
            by_channel.setdefault(channel_id, []).append(
                (user_id, date_str, int(stats.get("msg", 0)), int(stats.get("mention", 0)))
            )

        for channel_id, rows in by_channel.items():
            ensure_channel_db(channel_id)
            conn = sqlite3.connect(str(_channel_db_path(channel_id)))
            try:
                cur = conn.cursor()
                cur.execute("BEGIN")
                cur.executemany(
                    """
                    INSERT INTO daily_user_stats(user_id, date, msg_count, mention_count)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(user_id, date) DO UPDATE SET
                      msg_count = msg_count + excluded.msg_count,
                      mention_count = mention_count + excluded.mention_count
                    """,
                    [(str(uid), d, msg, men) for (uid, d, msg, men) in rows],
                )
                conn.commit()
            finally:
                conn.close()

    def _buffer_sum_stats(
        self, channel_id: int, user_id: int, since_date: str, until_date: str
    ) -> tuple[int, int]:
        msg_sum = 0
        mention_sum = 0
        for (ch_id, u_id, date_str), stats in self.buffer.items():
            if ch_id != channel_id or u_id != user_id:
                continue
            if date_str < since_date or date_str > until_date:
                continue
            msg_sum += int(stats.get("msg", 0))
            mention_sum += int(stats.get("mention", 0))
        return msg_sum, mention_sum

    async def query_stats(
        self, channel_id: int, user_id: int, period_days: int, now_utc: Optional[datetime] = None
    ) -> tuple[int, int]:
        """统计过去 N 天 msg/mention，总和=DB+buffer。"""

        since_date, until_date = _date_range_yyyymmdd(period_days, now_utc=now_utc)
        db_msg, db_mention = await asyncio.to_thread(
            _db_sum_stats, channel_id, user_id, since_date, until_date
        )
        buf_msg, buf_mention = self._buffer_sum_stats(channel_id, user_id, since_date, until_date)
        return db_msg + buf_msg, db_mention + buf_mention

    @tasks.loop(hours=3)
    async def flush_task(self) -> None:
        if not self.buffer:
            return

        items = list(self.buffer.items())
        # 先清空，避免 flush 过程中 on_message 写入导致重复；失败则回滚写回
        self.buffer = {}

        try:
            await asyncio.to_thread(self._flush_buffer_blocking, items)
        except Exception:
            LOG.exception("flush_task failed, restoring buffer")
            # 恢复：按 key 合并回去
            for k, v in items:
                cur = self.buffer.setdefault(k, {"msg": 0, "mention": 0})
                cur["msg"] += int(v.get("msg", 0))
                cur["mention"] += int(v.get("mention", 0))

    @flush_task.before_loop
    async def _before_flush_task(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_load(self) -> None:
        # 启动恢复 persistent views
        for panel_uuid, data in list(self.panels.items()):
            try:
                cfg = PanelConfig.from_dict(panel_uuid, data)
                self.bot.add_view(RoleAuditPanelView(self, panel_uuid), message_id=cfg.panel_message_id)
            except Exception:
                LOG.exception("Failed to restore panel view: %s", panel_uuid)

        # tasks 启动放在 cog_load，确保 event loop ready
        self.flush_task.start()
        self.expire_task.start()
        self.cleanup_task.start()

    async def cog_unload(self) -> None:
        self.flush_task.cancel()
        self.expire_task.cancel()
        self.cleanup_task.cancel()

        # 取消所有测试监听任务
        for _k, t in list(getattr(self, "_test_listen_tasks", {}).items()):
            try:
                t.cancel()
            except Exception:
                pass
        self._test_listen_tasks = {}

    def _format_listen_preview(self, msg: discord.Message) -> str:
        # 预览：前 10 个字符，纯图片/贴纸 [pic]，其他无文本内容 [other]
        content = (msg.content or "").strip()
        if content:
            preview = content[:10]
        else:
            is_pic = False
            try:
                # sticker
                if getattr(msg, "stickers", None):
                    if len(msg.stickers) > 0:
                        is_pic = True
                # attachments images
                if not is_pic and getattr(msg, "attachments", None):
                    for a in msg.attachments:
                        ct = (getattr(a, "content_type", None) or "").lower()
                        fn = (getattr(a, "filename", None) or "").lower()
                        if ct.startswith("image/") or fn.endswith(
                            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
                        ):
                            is_pic = True
                            break
            except Exception:
                is_pic = False

            if is_pic:
                preview = "[pic]"
            else:
                # 纯文件/嵌入等
                has_any_attachment = False
                try:
                    has_any_attachment = bool(getattr(msg, "attachments", None)) and len(msg.attachments) > 0
                except Exception:
                    has_any_attachment = False
                preview = "[other]" if has_any_attachment else ""

        ts = msg.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # 输出 ISO，便于复制
        ts_s = ts.astimezone(timezone.utc).isoformat()
        return f"author={int(msg.author.id)}\tmsg={int(msg.id)}\tts={ts_s}\t{preview}"

    def _get_panel_cfg(self, panel_uuid: str) -> Optional[PanelConfig]:
        data = self.panels.get(panel_uuid)
        if not data:
            return None
        try:
            return PanelConfig.from_dict(panel_uuid, data)
        except Exception:
            LOG.exception("Invalid panel config: %s", panel_uuid)
            return None

    def _can_manage_role(self, guild: discord.Guild, role: discord.Role) -> tuple[bool, str]:
        me = guild.me or guild.get_member(self.bot.user.id)  # type: ignore
        if me is None:
            return False, "无法获取 bot 成员对象"
        if not me.guild_permissions.manage_roles:
            return False, "bot 缺少 Manage Roles 权限"
        if role >= me.top_role:
            return False, f"bot 身份组层级不足（目标:{role.id} >= bot_top:{me.top_role.id}）"
        return True, "OK"

    def _validate_panel_thresholds(
        self,
        required_msg_count: int,
        required_mention_count: int,
        duration_days: int,
    ) -> list[str]:
        """校验面板参数。

        规则：
        - 发言数/提及数允许为 0（表示不设该项门槛），但两者不能同时为 0
        - duration_days 必须 > 0（不允许 0）
        """

        problems: list[str] = []

        if required_msg_count < 0:
            problems.append("required_msg_count 不能为负数")
        if required_mention_count < 0:
            problems.append("required_mention_count 不能为负数")

        if required_msg_count == 0 and required_mention_count == 0:
            problems.append("发言数要求与提及数要求不能同时为 0（至少设置一项门槛）")

        if duration_days <= 0:
            problems.append("duration_days 必须 > 0")

        return problems

    async def _grant_timed_role(
        self,
        guild: discord.Guild,
        member: discord.Member,
        grant_role_id: int,
        remove_role_id: int,
        duration_days: int,
        reason: str,
    ) -> tuple[bool, str]:
        """授予限时身份组：加 grant_role，移除 remove_role(若有)，写 timed_role_members。"""

        grant_role = guild.get_role(grant_role_id)
        if grant_role is None:
            return False, f"授予身份组不存在(ID:{grant_role_id})"

        remove_role = guild.get_role(remove_role_id) if remove_role_id else None

        ok, msg = self._can_manage_role(guild, grant_role)
        if not ok:
            return False, f"无法管理授予身份组：{msg}"
        if remove_role is not None:
            ok, msg = self._can_manage_role(guild, remove_role)
            if not ok:
                return False, f"无法管理移除身份组：{msg}"

        # add grant
        await member.add_roles(grant_role, reason=reason)
        await asyncio.sleep(1)

        restore_role_id: Optional[int] = None
        if remove_role is not None and remove_role in member.roles:
            await member.remove_roles(remove_role, reason=reason)
            restore_role_id = remove_role.id

        if duration_days > 0:
            ensure_timed_role_db()
            expire_ts = int(datetime.now(timezone.utc).timestamp() + duration_days * 86400)
            conn = sqlite3.connect(str(TIMED_ROLE_DB_PATH))
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO timed_role_members(user_id, role_id, expire_ts, restore_role_id, duration_days)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, role_id) DO UPDATE SET
                      expire_ts=excluded.expire_ts,
                      restore_role_id=excluded.restore_role_id,
                      duration_days=excluded.duration_days
                    """,
                    (
                        str(member.id),
                        str(grant_role.id),
                        int(expire_ts),
                        str(restore_role_id) if restore_role_id else None,
                        int(duration_days),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        return True, "OK"

    role_cfg_group = app_commands.Group(name="角色配置", description="RoleConfigure 管理命令")

    @role_cfg_group.command(name="测试审核面板", description="[admin]测试审核面板参数（不创建消息）")
    @app_commands.check(is_admin)
    @app_commands.guild_only()
    async def panel_test(
        self,
        interaction: discord.Interaction,
        required_role_id: int,
        stats_channel: discord.TextChannel,
        period_days: int,
        required_msg_count: int,
        required_mention_count: int,
        grant_role_id: int,
        remove_role_id: int,
        duration_days: int,
    ) -> None:
        await safe_defer(interaction)

        assert interaction.guild is not None
        guild = interaction.guild

        me = guild.me or guild.get_member(self.bot.user.id)  # type: ignore
        if me is None:
            await interaction.followup.send("无法获取 bot 成员对象", ephemeral=True)
            return

        perms = stats_channel.permissions_for(me)
        problems: list[str] = []
        if not perms.send_messages:
            problems.append("bot 在该频道缺少 send_messages")
        if not perms.embed_links:
            problems.append("bot 在该频道缺少 embed_links")

        grant_role = guild.get_role(grant_role_id)
        remove_role = guild.get_role(remove_role_id)
        required_role = guild.get_role(required_role_id)

        if required_role is None:
            problems.append(f"required_role 不存在(ID:{required_role_id})")
        if grant_role is None:
            problems.append(f"grant_role 不存在(ID:{grant_role_id})")
        else:
            ok, msg = self._can_manage_role(guild, grant_role)
            if not ok:
                problems.append(f"grant_role 不可管理：{msg}")
        if remove_role is None:
            problems.append(f"remove_role 不存在(ID:{remove_role_id})")
        else:
            ok, msg = self._can_manage_role(guild, remove_role)
            if not ok:
                problems.append(f"remove_role 不可管理：{msg}")

        if period_days <= 0:
            problems.append("period_days 必须 > 0")

        problems.extend(
            self._validate_panel_thresholds(
                required_msg_count=required_msg_count,
                required_mention_count=required_mention_count,
                duration_days=duration_days,
            )
        )

        if problems:
            await interaction.followup.send("\n".join(["测试未通过："] + [f"- {p}" for p in problems]), ephemeral=True)
        else:
            await interaction.followup.send("测试通过：权限与参数看起来都 OK。", ephemeral=True)

    @role_cfg_group.command(name="创建审核面板", description="[admin]创建审核面板（上传 JSON，带持久化按钮）")
    @app_commands.check(is_admin)
    @app_commands.guild_only()
    async def panel_create(
        self,
        interaction: discord.Interaction,
        config_json: discord.Attachment,
    ) -> None:
        await safe_defer(interaction)
        assert interaction.guild is not None

        # 读取并解析 JSON 附件
        if not (config_json.filename or "").lower().endswith(".json"):
            await interaction.followup.send("请上传 .json 文件（格式需与 example.json 一致）", ephemeral=True)
            return
        if config_json.size is not None and config_json.size > 256 * 1024:
            await interaction.followup.send("JSON 文件过大（>256KB）", ephemeral=True)
            return

        try:
            raw_bytes = await config_json.read()
        except Exception:
            await interaction.followup.send("读取附件失败，请重试", ephemeral=True)
            return

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            await interaction.followup.send("JSON 解析失败：请确认 UTF-8 编码且内容为合法 JSON", ephemeral=True)
            return

        if not isinstance(payload, dict):
            await interaction.followup.send("JSON 顶层必须是对象（{}）", ephemeral=True)
            return

        cfg_dict, problems = _parse_panel_json(payload)
        if problems or cfg_dict is None:
            await interaction.followup.send(
                "\n".join(["创建失败：JSON 校验未通过："] + [f"- {p}" for p in problems]),
                ephemeral=True,
            )
            return

        stats_channel_id = int(cfg_dict["stats_channel_id"])
        required_role_id = int(cfg_dict["required_role_id"])
        period_days = int(cfg_dict["period_days"])
        required_msg_count = int(cfg_dict["required_msg_count"])
        required_mention_count = int(cfg_dict["required_mention_count"])
        grant_role_id = int(cfg_dict["grant_role_id"])
        remove_role_id = int(cfg_dict["remove_role_id"])
        duration_days = int(cfg_dict["duration_days"])
        custom_title = str(cfg_dict["custom_title"])
        custom_desc = str(cfg_dict["custom_desc"])
        custom_button_text = str(cfg_dict["custom_button_text"])
        reason = str(cfg_dict["reason"])

        stats_channel = interaction.guild.get_channel(stats_channel_id)
        if not isinstance(stats_channel, discord.TextChannel):
            await interaction.followup.send(
                f"stats_channel_id 无效或不是文本频道：{stats_channel_id}",
                ephemeral=True,
            )
            return

        # 权限检查：bot 能在 stats_channel 发 embed
        me = interaction.guild.me or interaction.guild.get_member(self.bot.user.id)  # type: ignore
        if me is None:
            await interaction.followup.send("无法获取 bot 成员对象", ephemeral=True)
            return
        perms = stats_channel.permissions_for(me)
        if not perms.send_messages:
            await interaction.followup.send("bot 在 stats_channel 缺少 send_messages", ephemeral=True)
            return
        if not perms.embed_links:
            await interaction.followup.send("bot 在 stats_channel 缺少 embed_links", ephemeral=True)
            return

        panel_uuid = str(uuid.uuid4())
        view = RoleAuditPanelView(self, panel_uuid)

        embed = discord.Embed(title=custom_title, description=custom_desc, color=discord.Color.blurple())
        embed.add_field(name="统计频道", value=f"<#{stats_channel.id}>", inline=False)
        embed.add_field(name="统计周期(天)", value=str(period_days), inline=True)
        embed.add_field(name="发言数要求", value=str(required_msg_count), inline=True)
        embed.add_field(name="提及数要求", value=str(required_mention_count), inline=True)
        embed.add_field(name="授予身份组", value=f"<@&{grant_role_id}>", inline=False)
        embed.add_field(name="移除身份组", value=f"<@&{remove_role_id}>", inline=False)
        embed.add_field(name="有效期(天)", value=str(duration_days), inline=True)

        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.followup.send("当前频道不可发送消息", ephemeral=True)
            return

        msg = await channel.send(embed=embed, view=view)

        self.panels[panel_uuid] = {
            "guild_id": interaction.guild_id,
            "panel_channel_id": msg.channel.id,
            "panel_message_id": msg.id,
            "required_role_id": required_role_id,
            "stats_channel_id": stats_channel.id,
            "period_days": period_days,
            "required_msg_count": required_msg_count,
            "required_mention_count": required_mention_count,
            "grant_role_id": grant_role_id,
            "remove_role_id": remove_role_id,
            "duration_days": duration_days,
            "reason": reason,
            "custom_title": custom_title,
            "custom_desc": custom_desc,
            "custom_button_text": custom_button_text,
        }
        save_panels(self.panels)

        # 注册 persistent view
        self.bot.add_view(RoleAuditPanelView(self, panel_uuid), message_id=msg.id)

        await interaction.followup.send(f"已创建面板：{panel_uuid}", ephemeral=True)

    @role_cfg_group.command(name="直接授予限时身份组", description="[admin]直接授予限时身份组")
    @app_commands.check(is_admin)
    @app_commands.guild_only()
    async def grant_timed_role_admin(
        self,
        interaction: discord.Interaction,
        role_id: int,
        user_ids: str,
        duration_days: Optional[int] = None,
        remove_role_id: Optional[int] = None,
    ) -> None:
        await safe_defer(interaction)
        assert interaction.guild is not None
        guild = interaction.guild

        targets: list[int] = []
        for part in user_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                targets.append(int(part))
            except ValueError:
                await interaction.followup.send(f"用户ID解析失败：{part}", ephemeral=True)
                return

        if not targets:
            await interaction.followup.send("未提供任何 user_ids", ephemeral=True)
            return

        if duration_days is None:
            duration_days_int = 0
        else:
            duration_days_int = int(duration_days)
            if duration_days_int < 0:
                await interaction.followup.send("duration_days 不能为负数", ephemeral=True)
                return

        reason = f"管理员直接授予限时身份组（ID:{role_id}）"
        success = 0
        failed: list[str] = []

        for idx, uid in enumerate(targets):
            try:
                member = guild.get_member(uid)
                if member is None:
                    member = await guild.fetch_member(uid)
            except Exception:
                failed.append(f"{uid}: 找不到成员")
                await asyncio.sleep(10)
                continue

            try:
                ok, msg = await self._grant_timed_role(
                    guild=guild,
                    member=member,
                    grant_role_id=role_id,
                    remove_role_id=int(remove_role_id or 0),
                    duration_days=duration_days_int,
                    reason=reason,
                )
                if ok:
                    success += 1
                else:
                    failed.append(f"{uid}: {msg}")
            except Exception as e:
                failed.append(f"{uid}: 异常 {e}")

            # 同用户内已有 1 秒间隔；不同用户之间 10 秒
            await asyncio.sleep(10)

        msg_lines = [f"完成：成功 {success}/{len(targets)}"]
        if failed:
            msg_lines.append("失败明细：")
            msg_lines.extend([f"- {x}" for x in failed[:20]])
            if len(failed) > 20:
                msg_lines.append(f"...(共{len(failed)}条)")

        await interaction.followup.send("\n".join(msg_lines), ephemeral=True)

    @role_cfg_group.command(name="编辑限时身份组有效期", description="[admin]调整某用户限时身份组的有效期（天）")
    @app_commands.check(is_admin)
    @app_commands.guild_only()
    async def edit_timed_role_duration(
        self,
        interaction: discord.Interaction,
        role_id: int,
        user_id: int,
        delta_days: int,
    ) -> None:
        await safe_defer(interaction)
        ensure_timed_role_db()
        now_ts = int(datetime.now(timezone.utc).timestamp())

        conn = sqlite3.connect(str(TIMED_ROLE_DB_PATH))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT expire_ts, restore_role_id, duration_days
                FROM timed_role_members
                WHERE user_id=? AND role_id=?
                """,
                (str(user_id), str(role_id)),
            )
            row = cur.fetchone()
            if not row:
                await interaction.followup.send("未找到该限时身份组记录", ephemeral=True)
                return

            old_expire_ts = int(row[0])
            restore_role_id_s = row[1]
            old_duration_days = int(row[2] or 0)

            new_expire_ts = old_expire_ts + int(delta_days) * 86400
            new_duration_days = old_duration_days + int(delta_days)

            if new_expire_ts <= now_ts:
                # 立即撤销
                cur.execute(
                    "DELETE FROM timed_role_members WHERE user_id=? AND role_id=?",
                    (str(user_id), str(role_id)),
                )
                conn.commit()

                if interaction.guild is None:
                    await interaction.followup.send("已删除记录（非服务器环境无法执行撤销）", ephemeral=True)
                    return

                guild = interaction.guild
                try:
                    member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                except Exception:
                    await interaction.followup.send(
                        "已删除记录，但找不到成员执行撤销", ephemeral=True
                    )
                    return

                role = guild.get_role(role_id)
                restore_role = (
                    guild.get_role(int(restore_role_id_s)) if restore_role_id_s else None
                )
                reason = f"限时身份组（身份组ID:{role_id}）被admin手动提前结束"
                try:
                    if role is not None and role in member.roles:
                        await member.remove_roles(role, reason=reason)
                        await asyncio.sleep(1)
                    if restore_role is not None and restore_role not in member.roles:
                        await member.add_roles(restore_role, reason=reason)
                except Exception:
                    LOG.exception("Failed to early-end timed role user=%s role=%s", user_id, role_id)

                await interaction.followup.send("已提前结束并撤销该限时身份组", ephemeral=True)
                return

            cur.execute(
                """
                UPDATE timed_role_members
                SET expire_ts=?, duration_days=?
                WHERE user_id=? AND role_id=?
                """,
                (int(new_expire_ts), int(new_duration_days), str(user_id), str(role_id)),
            )
            conn.commit()
        finally:
            conn.close()

        await interaction.followup.send(
            f"已更新有效期：expire_ts {new_expire_ts}，duration_days {new_duration_days}",
            ephemeral=True,
        )

    @role_cfg_group.command(name="更新频道数据库", description="[admin]增量统计频道历史消息写入数据库")
    @app_commands.check(is_admin)
    @app_commands.guild_only()
    async def update_channel_db(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        mode: Optional[str] = None,
        filter_mode: Optional[str] = None,
        preview: Optional[bool] = False,
        message_count: int = 0,
    ) -> None:
        await safe_defer(interaction)

        mode_norm = str((mode if mode else "增量")).strip()
        if mode_norm not in {"初始化", "增量"}:
            await interaction.followup.send("mode 只能是 '初始化' 或 '增量'", ephemeral=True)
            return

        filter_mode_norm = str((filter_mode if filter_mode else "开启过滤")).strip()
        if filter_mode_norm not in {"开启过滤", "关闭过滤"}:
            await interaction.followup.send("filter_mode 只能是 '开启过滤' 或 '关闭过滤'", ephemeral=True)
            return
        enable_filters = filter_mode_norm == "开启过滤"
        preview_enabled = bool(preview)
        if message_count <= 0:
            await interaction.followup.send("message_count 必须 > 0", ephemeral=True)
            return

        # 初始化：二次确认 + 清空并重建该频道 DB
        if mode_norm == "初始化":
            confirm_view = ConfirmActionView(author_id=int(interaction.user.id))
            await interaction.followup.send(
                "你选择了【初始化】模式：将清空该频道统计数据库并重新写入。\n"
                "这会导致历史统计被重算，确定继续吗？",
                ephemeral=True,
                view=confirm_view,
            )
            try:
                await confirm_view.wait()
            except Exception:
                pass
            if not confirm_view.confirmed:
                await interaction.followup.send("已取消初始化。", ephemeral=True)
                return

            # 尽量先 flush buffer，避免“实时统计 buffer”与初始化写库交叉导致重复
            if self.buffer:
                items = list(self.buffer.items())
                self.buffer = {}
                try:
                    await asyncio.to_thread(self._flush_buffer_blocking, items)
                except Exception:
                    LOG.exception("Failed to flush buffer before init update_channel_db")
                    # flush 失败则恢复 buffer
                    for k, v in items:
                        cur = self.buffer.setdefault(k, {"msg": 0, "mention": 0})
                        cur["msg"] += int(v.get("msg", 0))
                        cur["mention"] += int(v.get("mention", 0))

            ensure_channel_db(channel.id)
            db_path = _channel_db_path(channel.id)

            def _reset_db() -> None:
                conn = sqlite3.connect(str(db_path))
                try:
                    cur = conn.cursor()
                    cur.execute("BEGIN")
                    cur.execute("DELETE FROM daily_user_stats")
                    cur.execute("DELETE FROM meta")
                    conn.commit()
                finally:
                    conn.close()

            try:
                await asyncio.to_thread(_reset_db)
            except Exception:
                LOG.exception("Failed to reset channel db for init")
                await interaction.followup.send("初始化失败：清空数据库时出错，请查看日志。", ephemeral=True)
                return

        # 先发一个“临时消息”，后续用 edit 方式更新进度，避免用户端一直显示“正在响应…”
        progress_msg = await interaction.followup.send(
            f"开始更新频道数据库（{mode_norm} / {filter_mode_norm}）：#{channel.name} ({channel.id})\n"
            f"目标处理：{message_count} 条消息\n（过程中会定期更新进度）",
            ephemeral=True,
            wait=True,
        )

        ensure_channel_db(channel.id)
        db_path = _channel_db_path(channel.id)

        def _get_last_id() -> int:
            conn = sqlite3.connect(str(db_path))
            try:
                last = _meta_get(conn, "last_message_id")
                return int(last) if last else 0
            finally:
                conn.close()

        last_message_id = await asyncio.to_thread(_get_last_id)

        # 初始化模式：不做 last_message_id 过滤，强制全量按本次抓取重算
        # 关闭过滤模式：也不做 last_message_id 过滤
        if mode_norm == "初始化" or (not enable_filters):
            last_message_id = 0

        # 拉取最近 N 条消息（倒序），筛掉已处理过的
        # 由于 Discord API 会对 history 拉取做限流（429），这里增加“软冷却”以降低触发频率：
        # - 每处理 100 条消息 sleep 5 秒
        # - 每处理 3000 条消息更新一次进度（编辑临时消息）
        new_msgs: list[discord.Message] = []
        fetched = 0
        accepted = 0
        max_id = last_message_id

        async def _edit_progress(force: bool = False) -> None:
            """编辑临时进度消息（ephemeral）。"""

            # progress_msg 理论上一定存在；这里做保护以防意外
            if progress_msg is None:
                return
            if not force and fetched % 3000 != 0:
                return
            try:
                await progress_msg.edit(
                    content=(
                        f"正在更新频道数据库：#{channel.name} ({channel.id})\n"
                        f"- 已抓取：{fetched}/{message_count}\n"
                        f"- 新增候选：{fetched}\n"
                        f"- 计入统计：{accepted}\n"
                        f"- last_message_id：{last_message_id}\n"
                        f"- 当前 max_message_id：{max_id}"
                    )
                )
            except Exception:
                # 编辑失败不影响主流程
                LOG.exception("Failed to edit progress message")

        async for msg in channel.history(limit=message_count, oldest_first=False):
            fetched += 1
            if msg.id > max_id:
                max_id = msg.id

            if enable_filters:
                if msg.id <= last_message_id:
                    # 已处理过（或更早），不再计入
                    await _edit_progress()
                    if fetched % 100 == 0:
                        await asyncio.sleep(5)
                    continue
                if msg.author.bot:
                    await _edit_progress()
                    if fetched % 100 == 0:
                        await asyncio.sleep(5)
                    continue
                # 统计：普通消息 + 回复消息（reply）
                if msg.type not in {discord.MessageType.default, discord.MessageType.reply}:
                    await _edit_progress()
                    if fetched % 100 == 0:
                        await asyncio.sleep(5)
                    continue

            new_msgs.append(msg)
            accepted += 1

            await _edit_progress()
            if fetched % 100 == 0:
                await asyncio.sleep(5)

        if not new_msgs:
            try:
                await progress_msg.edit(
                    content=(
                        f"无需更新：最近 {fetched} 条消息中没有新消息\n"
                        f"- last_message_id={last_message_id}\n"
                        f"- 目标处理={message_count}"
                    )
                )
            except Exception:
                pass
            return

        # 统计聚合：key=(user_id, date)
        agg: dict[tuple[int, str], dict[str, int]] = {}
        for msg in new_msgs:
            date_str = get_bj_date_str(msg.created_at)
            k = (msg.author.id, date_str)
            e = agg.setdefault(k, {"msg": 0, "mention": 0})
            e["msg"] += 1
            if msg.mentions or msg.role_mentions:
                e["mention"] += 1

        # 预览：前 200 条被统计入库的消息（按抓取顺序：history 是倒序，因此这里也是从“最新”往“更早”）
        preview_lines: list[str] = []
        if preview_enabled:
            preview_lines.append(
                f"channel={channel.name} ({channel.id}) mode={mode_norm} filter={filter_mode_norm} fetched={fetched} accepted={accepted}"
            )
            preview_lines.append("格式：author_id\tcontent_head")
            for m in new_msgs[:200]:
                head: str
                content = (m.content or "").strip()
                if content:
                    head = content[:6]
                else:
                    # 无文本：如果只有图片/附件则标记 [pic]
                    has_pic = False
                    try:
                        if getattr(m, "attachments", None):
                            for a in m.attachments:
                                ct = (getattr(a, "content_type", None) or "").lower()
                                fn = (getattr(a, "filename", None) or "").lower()
                                if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                                    has_pic = True
                                    break
                    except Exception:
                        has_pic = False

                    head = "[pic]" if has_pic else ""

                preview_lines.append(f"{int(m.author.id)}\t{head}")

        def _write_agg() -> None:
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                cur.execute("BEGIN")
                cur.executemany(
                    """
                    INSERT INTO daily_user_stats(user_id, date, msg_count, mention_count)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(user_id, date) DO UPDATE SET
                      msg_count = msg_count + excluded.msg_count,
                      mention_count = mention_count + excluded.mention_count
                    """,
                    [
                        (str(uid), date, int(v["msg"]), int(v["mention"]))
                        for (uid, date), v in agg.items()
                    ],
                )
                # 关闭过滤时不推进 last_message_id，避免“误推进”导致后续增量漏算。
                if enable_filters:
                    _meta_set(conn, "last_message_id", str(max_id))
                conn.commit()
            finally:
                conn.close()

        try:
            await progress_msg.edit(
                content=(
                    f"抓取完成，正在写入数据库：#{channel.name} ({channel.id})\n"
                    f"- 已抓取：{fetched}/{message_count}\n"
                    f"- 计入统计：{accepted}\n"
                    f"- 写入聚合条目（user_id+date）：{len(agg)}\n"
                    f"- last_message_id：{last_message_id} -> {max_id if enable_filters else last_message_id}"
                )
            )
        except Exception:
            pass

        await asyncio.to_thread(_write_agg)

        # 预览文件：私信发送 txt
        preview_dm_ok: Optional[bool] = None
        if preview_enabled:
            try:
                content_txt = "\n".join(preview_lines) + "\n"
                preview_file = discord.File(
                    fp=io.BytesIO(content_txt.encode("utf-8")),
                    filename=f"role_configure_preview_{channel.id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt",
                )
                await interaction.user.send(
                    content=(
                        f"预览（前{min(200, len(new_msgs))}条被统计入库的消息）：#{channel.name} ({channel.id})\n"
                        f"mode={mode_norm} filter={filter_mode_norm}"
                    ),
                    file=preview_file,
                )
                preview_dm_ok = True
            except Exception:
                LOG.exception("Failed to send preview txt")
                preview_dm_ok = False

        # 尽量私信通知
        dm_ok = True
        try:
            await interaction.user.send(
                f"频道数据库更新完成：#{channel.name} ({channel.id})\n"
                f"- 本次抓取：{fetched} 条\n"
                f"- 新增统计消息：{len(new_msgs)} 条\n"
                f"- last_message_id: {last_message_id} -> {max_id if enable_filters else last_message_id}"
            )
        except Exception:
            dm_ok = False

        try:
            await progress_msg.edit(
                content=(
                    f"更新完成：#{channel.name} ({channel.id})\n"
                    f"- 本次抓取：{fetched} 条\n"
                    f"- 新增统计消息：{len(new_msgs)} 条\n"
                    f"- last_message_id: {last_message_id} -> {max_id if enable_filters else last_message_id}\n"
                    f"{'（已私信通知）' if dm_ok else '（私信失败）'}"
                    + (
                        "\n预览：" + ("已发送" if (preview_dm_ok is True) else "发送失败")
                        if preview_enabled
                        else ""
                    )
                )
            )
        except Exception:
            # 兜底：编辑失败则再发一条
            await interaction.followup.send(
                (
                    f"更新完成：新增统计 {len(new_msgs)} 条消息。"
                    f"{'（已私信通知）' if dm_ok else '（私信失败）'}"
                    + (
                        f" 预览：{('已发送' if (preview_dm_ok is True) else '发送失败')}"
                        if preview_enabled
                        else ""
                    )
                ),
                ephemeral=True,
            )

    @update_channel_db.autocomplete("mode")
    async def _update_channel_db_mode_ac(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        options = [
            app_commands.Choice(name="初始化", value="初始化"),
            app_commands.Choice(name="增量", value="增量"),
        ]
        c = (current or "").strip()
        return [x for x in options if (not c) or (c in x.name) or (c in x.value)]

    @update_channel_db.autocomplete("filter_mode")
    async def _update_channel_db_filter_mode_ac(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        options = [
            app_commands.Choice(name="开启过滤（默认：过滤bot/非普通消息/已统计ID）", value="开启过滤"),
            app_commands.Choice(name="关闭过滤（危险：全算，且不更新last_message_id）", value="关闭过滤"),
        ]
        c = (current or "").strip()
        return [x for x in options if (not c) or (c in x.name) or (c in x.value)]

    @role_cfg_group.command(name="测试频道监听", description="[admin]临时监听某频道消息，并私信回传消息信息")
    @app_commands.check(is_admin)
    @app_commands.guild_only()
    async def test_channel_listen(
        self,
        interaction: discord.Interaction,
        channel_id: str,
        mode: Optional[str] = None,
        minutes: Optional[int] = None,
        n: Optional[int] = None,
    ) -> None:
        """临时监听：定时（<=30分钟）或 n 条消息（<=100）。

        参数说明：
        - channel_id: 频道 snowflake（用 str，避免 32-bit 限制）
        - mode: '定时' | 'n条消息'
        - minutes: 定时分钟数（mode=定时 时必填）
        - n: 消息条数（mode=n条消息 时必填）
        """

        await safe_defer(interaction)
        assert interaction.guild is not None

        try:
            channel_id_int = int(str(channel_id).strip())
        except Exception:
            await interaction.followup.send("channel_id 必须是纯数字ID（snowflake）。", ephemeral=True)
            return

        mode_norm = str((mode if mode else "定时")).strip()
        if mode_norm not in {"定时", "n条消息"}:
            await interaction.followup.send("mode 只能是 '定时' 或 'n条消息'", ephemeral=True)
            return

        # 校验并准备结束条件
        duration_s: Optional[int] = None
        target_n: Optional[int] = None
        if mode_norm == "定时":
            if minutes is None:
                await interaction.followup.send("定时模式需要提供 minutes", ephemeral=True)
                return
            m = int(minutes)
            if m <= 0:
                await interaction.followup.send("minutes 必须 > 0", ephemeral=True)
                return
            if m > 30:
                await interaction.followup.send("minutes 不能大于 30", ephemeral=True)
                return
            duration_s = m * 60
        else:
            if n is None:
                await interaction.followup.send("n条消息模式需要提供 n", ephemeral=True)
                return
            nn = int(n)
            if nn <= 0:
                await interaction.followup.send("n 必须 > 0", ephemeral=True)
                return
            if nn > 100:
                await interaction.followup.send("n 不能大于 100", ephemeral=True)
                return
            target_n = nn

        # 频道存在性与权限检查
        ch = interaction.guild.get_channel(channel_id_int)
        if not isinstance(ch, discord.TextChannel):
            await interaction.followup.send(
                f"找不到文本频道：{channel_id_int}（请确认ID正确且 bot 能看到该频道）",
                ephemeral=True,
            )
            return

        me = interaction.guild.me or interaction.guild.get_member(self.bot.user.id)  # type: ignore
        if me is None:
            await interaction.followup.send("无法获取 bot 成员对象", ephemeral=True)
            return
        perms = ch.permissions_for(me)
        if not perms.read_messages:
            await interaction.followup.send("bot 在该频道缺少 read_messages/view_channel 权限", ephemeral=True)
            return
        if not perms.read_message_history:
            await interaction.followup.send("bot 在该频道缺少 read_message_history 权限", ephemeral=True)
            return

        requester_id = int(interaction.user.id) if interaction.user is not None else 0
        key = (requester_id, int(ch.id))
        if key in self._test_listen_tasks:
            await interaction.followup.send("你已经在监听该频道了，请等待结束后再试。", ephemeral=True)
            return

        # 实际监听任务
        async def _runner() -> None:
            collected: list[discord.Message] = []
            started_at = datetime.now(timezone.utc)

            q: asyncio.Queue[discord.Message] = asyncio.Queue()

            async def _inject(message: discord.Message) -> None:
                # 排除 bot 消息，避免噪音
                try:
                    if message.author and getattr(message.author, "bot", False):
                        return
                except Exception:
                    pass
                await q.put(message)

            # 挂到 task 上供 on_message 注入
            t = asyncio.current_task()
            assert t is not None
            setattr(t, "_rc_inject_message", _inject)

            async def _finish_and_dm(reason_text: str) -> None:
                lines = [
                    f"测试频道监听结束：#{ch.name} ({ch.id})",
                    f"结束原因：{reason_text}",
                    f"开始时间(UTC)：{started_at.isoformat()}",
                    f"收集到消息数：{len(collected)}",
                    "\n格式：author_id\tmsg_id\tts(UTC)\tpreview",
                ]
                for m in collected:
                    lines.append(self._format_listen_preview(m))
                content_txt = "\n".join(lines) + "\n"
                try:
                    f = discord.File(
                        fp=io.BytesIO(content_txt.encode("utf-8")),
                        filename=f"test_channel_listen_{ch.id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt",
                    )
                    await interaction.user.send(
                        content=f"频道监听结果文件（{mode_norm}）：#{ch.name} ({ch.id})",
                        file=f,
                    )
                except Exception:
                    LOG.exception("Failed to DM listen result")
                    # DM 失败兜底：不在频道内公开
                    try:
                        await interaction.followup.send("监听结束，但私信发送失败（可能未开启私信）。", ephemeral=True)
                    except Exception:
                        pass

            try:
                if duration_s is not None:
                    # 定时模式：直到超时
                    deadline = asyncio.get_event_loop().time() + float(duration_s)
                    while True:
                        timeout = max(0.1, deadline - asyncio.get_event_loop().time())
                        if timeout <= 0:
                            break
                        try:
                            msg = await asyncio.wait_for(q.get(), timeout=timeout)
                        except asyncio.TimeoutError:
                            break
                        collected.append(msg)
                else:
                    # n 条模式
                    assert target_n is not None
                    while len(collected) < target_n:
                        msg = await q.get()
                        collected.append(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("listen runner crashed")
                await _finish_and_dm("异常中断（请查看日志）")
                return

            # 正常结束
            if duration_s is not None:
                await _finish_and_dm(f"到达定时上限 {int(duration_s // 60)} 分钟")
            else:
                await _finish_and_dm(f"已收到 {target_n} 条消息")

        task = asyncio.create_task(_runner())
        self._test_listen_tasks[key] = task

        def _cleanup(_t: asyncio.Task[None]) -> None:
            try:
                self._test_listen_tasks.pop(key, None)
            except Exception:
                pass

        task.add_done_callback(_cleanup)

        # 立即给用户反馈
        if mode_norm == "定时":
            assert duration_s is not None
            await interaction.followup.send(
                f"已开始监听：#{ch.name} ({ch.id})，持续 {int(duration_s // 60)} 分钟。结束后会私信你结果。",
                ephemeral=True,
            )
        else:
            assert target_n is not None
            await interaction.followup.send(
                f"已开始监听：#{ch.name} ({ch.id})，直到收到 {int(target_n)} 条消息。结束后会私信你结果。",
                ephemeral=True,
            )

    @test_channel_listen.autocomplete("mode")
    async def _test_channel_listen_mode_ac(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        options = [
            app_commands.Choice(name="定时（minutes<=30）", value="定时"),
            app_commands.Choice(name="n条消息（n<=100）", value="n条消息"),
        ]
        c = (current or "").strip()
        return [x for x in options if (not c) or (c in x.name) or (c in x.value)]

    @role_cfg_group.command(
        name="查询频道数据库",
        description="[admin]查询某用户在某频道对应统计数据库中的数据（按天明细与汇总）",
    )
    @app_commands.check(is_admin)
    @app_commands.guild_only()
    async def query_channel_db(
        self,
        interaction: discord.Interaction,
        channel_id: str,
        user_id: str,
        days: Optional[int] = 30,
    ) -> None:
        await safe_defer(interaction)

        # Discord 的 app_commands integer 选项是 32-bit，有些 snowflake（频道/用户ID）会超范围。
        # 因此这里使用 str 接收，再自行转 int 校验。
        try:
            channel_id_int = int(str(channel_id).strip())
            user_id_int = int(str(user_id).strip())
        except Exception:
            await interaction.followup.send(
                "channel_id 与 user_id 必须是纯数字ID（snowflake）。",
                ephemeral=True,
            )
            return

        # 仅允许查询已存在的频道 DB（没有对应 db 文件则拒绝）
        _ensure_role_configure_dir()
        db_path = _channel_db_path(channel_id_int)
        if not db_path.exists():
            await interaction.followup.send(
                f"该频道没有对应统计数据库文件：{db_path}（channel_id={channel_id}）",
                ephemeral=True,
            )
            return

        days_int = int(days or 30)
        if days_int <= 0:
            await interaction.followup.send("days 必须 > 0", ephemeral=True)
            return
        if days_int > 180:
            days_int = 180

        since_date, until_date = _date_range_yyyymmdd(days_int)

        # buffer（尚未 flush 到 DB 的实时统计）
        buf_msg, buf_mention = self._buffer_sum_stats(channel_id_int, user_id_int, since_date, until_date)

        def _query() -> tuple[list[tuple[str, int, int]], int, int]:
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT date, msg_count, mention_count
                    FROM daily_user_stats
                    WHERE user_id=? AND date>=? AND date<=?
                    ORDER BY date DESC
                    """,
                    (str(user_id_int), since_date, until_date),
                )
                rows = [(str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in cur.fetchall()]

                cur.execute(
                    """
                    SELECT COALESCE(SUM(msg_count), 0), COALESCE(SUM(mention_count), 0)
                    FROM daily_user_stats
                    WHERE user_id=? AND date>=? AND date<=?
                    """,
                    (str(user_id_int), since_date, until_date),
                )
                srow = cur.fetchone() or (0, 0)
                total_msg = int(srow[0] or 0)
                total_mention = int(srow[1] or 0)
                return rows, total_msg, total_mention
            finally:
                conn.close()

        try:
            rows, total_msg, total_mention = await asyncio.to_thread(_query)
        except Exception:
            LOG.exception("query_channel_db failed channel_id=%s user_id=%s", channel_id, user_id)
            await interaction.followup.send("查询失败：读取数据库时发生错误，请查看日志。", ephemeral=True)
            return

        header = (
            f"频道数据库查询结果（channel_id={channel_id_int} / user_id={user_id_int}）\n"
            f"时间范围（北京时间）：{since_date} ~ {until_date}\n"
            f"汇总（DB）：msg={total_msg}，mention={total_mention}\n"
            f"汇总（buffer 未落库）：msg={buf_msg}，mention={buf_mention}\n"
            f"汇总（DB+buffer）：msg={total_msg + buf_msg}，mention={total_mention + buf_mention}\n"
        )

        if not rows:
            await interaction.followup.send(header + "\n该时间范围内无记录。", ephemeral=True)
            return

        # 输出最近 20 天明细，避免消息过长
        lines = ["按天明细（最多显示 20 条，date=YYYYMMDD）："]
        for d, m, me in rows[:20]:
            lines.append(f"- {d}: msg={m}, mention={me}")
        if len(rows) > 20:
            lines.append(f"...(共{len(rows)}条)")

        await interaction.followup.send(header + "\n" + "\n".join(lines), ephemeral=True)

    @tasks.loop(minutes=10)
    async def expire_task(self) -> None:
        ensure_timed_role_db()
        now_ts = int(datetime.now(timezone.utc).timestamp())

        rows: list[tuple[str, str, int, Optional[str], int]] = []
        conn = sqlite3.connect(str(TIMED_ROLE_DB_PATH))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT user_id, role_id, expire_ts, restore_role_id, duration_days
                FROM timed_role_members
                WHERE expire_ts <= ?
                ORDER BY expire_ts ASC
                """,
                (now_ts,),
            )
            rows = [(r[0], r[1], int(r[2]), r[3], int(r[4] or 0)) for r in cur.fetchall()]
        finally:
            conn.close()

        if not rows:
            return

        for user_id_s, role_id_s, _expire_ts, restore_role_id_s, duration_days in rows:
            user_id = int(user_id_s)
            role_id = int(role_id_s)
            restore_role_id = int(restore_role_id_s) if restore_role_id_s else None

            reason = (
                f"限时身份组（身份组ID:{role_id}）在（{duration_days}天）后到期"
                if duration_days > 0
                else f"限时身份组（身份组ID:{role_id}）已到期"
            )

            removed_any = False

            # 未保存 guild_id 的兼容方案：遍历所有 guild 尝试定位 member
            for guild in list(self.bot.guilds):
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except Exception:
                        continue

                role = guild.get_role(role_id)
                if role is None:
                    continue

                try:
                    if role in member.roles:
                        await member.remove_roles(role, reason=reason)
                        removed_any = True
                        await asyncio.sleep(1)

                    if restore_role_id is not None:
                        restore_role = guild.get_role(restore_role_id)
                        if restore_role is not None and restore_role not in member.roles:
                            await member.add_roles(restore_role, reason=reason)
                except Exception:
                    LOG.exception("Failed to revoke timed role user=%s role=%s", user_id, role_id)

            # 清理 DB 记录（无论是否成功移除，避免无限重试；出错可人工补）
            try:
                conn2 = sqlite3.connect(str(TIMED_ROLE_DB_PATH))
                cur2 = conn2.cursor()
                cur2.execute(
                    "DELETE FROM timed_role_members WHERE user_id=? AND role_id=?",
                    (str(user_id), str(role_id)),
                )
                conn2.commit()
            finally:
                try:
                    conn2.close()
                except Exception:
                    pass

            # 不同用户/记录之间 10 秒间隔（审计与限流友好）
            await asyncio.sleep(10)

    @expire_task.before_loop
    async def _before_expire_task(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(time=time(hour=4, minute=0, tzinfo=BJ_TZ))
    async def cleanup_task(self) -> None:
        cutoff_date = (datetime.now(timezone.utc).astimezone(BJ_TZ) - timedelta(days=100)).strftime(
            "%Y%m%d"
        )

        def _cleanup_one(db_path: Path) -> int:
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM daily_user_stats WHERE date < ?", (cutoff_date,))
                deleted = cur.rowcount if cur.rowcount is not None else 0
                conn.commit()
                return int(deleted)
            finally:
                conn.close()

        _ensure_role_configure_dir()
        deleted_total = 0
        for p in ROLE_CONFIGURE_DIR.glob("*.db"):
            if p.name == TIMED_ROLE_DB_PATH.name:
                continue
            try:
                deleted_total += await asyncio.to_thread(_cleanup_one, p)
            except Exception:
                LOG.exception("cleanup_task failed for %s", p)

        LOG.info("cleanup_task done, cutoff=%s deleted_total=%s", cutoff_date, deleted_total)

    @cleanup_task.before_loop
    async def _before_cleanup_task(self) -> None:
        await self.bot.wait_until_ready()


class RoleAuditPanelView(discord.ui.View):
    def __init__(self, cog: RoleConfigure, panel_uuid: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.panel_uuid = panel_uuid

        cfg = cog._get_panel_cfg(panel_uuid)
        label = "审核领取"
        if cfg is not None:
            raw = cog.panels.get(panel_uuid) or {}
            label = str(raw.get("custom_button_text") or label)

        self.add_item(RoleAuditButton(panel_uuid, label=label))


class RoleAuditButton(discord.ui.Button):
    def __init__(self, panel_uuid: str, label: str = "审核领取"):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"role_panel:{panel_uuid}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        if interaction.guild is None:
            await interaction.followup.send("仅支持服务器内使用", ephemeral=True)
            return

        assert isinstance(interaction.user, discord.Member)
        member: discord.Member = interaction.user

        view = self.view
        if not isinstance(view, RoleAuditPanelView):
            await interaction.followup.send("面板视图异常，请联系管理员", ephemeral=True)
            return

        cfg = view.cog._get_panel_cfg(view.panel_uuid)
        if cfg is None:
            await interaction.followup.send("该面板配置不存在/已失效", ephemeral=True)
            return

        guild = interaction.guild
        required_role = guild.get_role(cfg.required_role_id)
        if required_role is None:
            await interaction.followup.send(f"前置身份组不存在(ID:{cfg.required_role_id})", ephemeral=True)
            return
        if required_role not in member.roles:
            await interaction.followup.send(f"缺少前置身份组(ID:{cfg.required_role_id})", ephemeral=True)
            return

        grant_role = guild.get_role(cfg.grant_role_id)
        if grant_role is not None and grant_role in member.roles:
            await interaction.followup.send("你已拥有该身份组，无法重复领取。", ephemeral=True)
            return

        # 统计：频道来自面板配置 stats_channel_id（同时也是 role_configure 统计 DB 的 channel_id）
        msg_count, mention_count = await view.cog.query_stats(
            cfg.stats_channel_id, member.id, cfg.period_days
        )

        # 允许某一项要求为 0（表示不设门槛），但两项都为 0 的面板应在创建阶段被拒绝。
        # 这里仍做兼容：若历史面板两项都为 0，则直接拒绝。
        if cfg.required_msg_count == 0 and cfg.required_mention_count == 0:
            await interaction.followup.send(
                "该面板配置无效：发言数要求与提及数要求不能同时为 0，请联系管理员重新创建。",
                ephemeral=True,
            )
            return

        msg_ok = True if cfg.required_msg_count == 0 else (msg_count >= cfg.required_msg_count)
        mention_ok = True if cfg.required_mention_count == 0 else (
            mention_count >= cfg.required_mention_count
        )

        if not (msg_ok and mention_ok):
            await interaction.followup.send(
                f"未满足条件：\n"
                f"- 发言数：{msg_count}/{cfg.required_msg_count}\n"
                f"- 提及数：{mention_count}/{cfg.required_mention_count}",
                ephemeral=True,
            )
            return

        ok, msg = await view.cog._grant_timed_role(
            guild=guild,
            member=member,
            grant_role_id=cfg.grant_role_id,
            remove_role_id=cfg.remove_role_id,
            duration_days=cfg.duration_days,
            reason=cfg.reason or "RoleConfigure 面板授予",
        )
        if not ok:
            await interaction.followup.send(f"授予失败：{msg}", ephemeral=True)
            return

        await interaction.followup.send(
            f"审核通过，已授予身份组 <@&{cfg.grant_role_id}>（有效期 {cfg.duration_days} 天）。",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleConfigure(bot))
