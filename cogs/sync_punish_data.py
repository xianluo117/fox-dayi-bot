import discord
from discord.ext import commands
import os
import json
import sqlite3
from dotenv import load_dotenv
from datetime import datetime
from typing import Optional

# 加载环境变量
load_dotenv()


class SyncPunishDataCog(commands.Cog):
    """监听对接频道的Bot消息以同步处罚记录"""

    def __init__(self, bot):
        self.bot = bot
        self.interface_channel_id = self._parse_int(os.getenv("QUICK_PUNISH_INTERFACE_CHANNEL"))
        self.interface_bot_id = self._parse_int(os.getenv("QUICK_PUNISH_INTERFACE_BOT_ID"))
        self.init_database()

    def _parse_int(self, s: Optional[str]) -> Optional[int]:
        try:
            if s is None:
                return None
            return int(str(s).strip())
        except Exception:
            return None

    def init_database(self):
        """幂等建表，保证跨模块顺序加载安全"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quick_punish_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                punish_count INTEGER DEFAULT 1,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                original_message_id TEXT,
                original_message_link TEXT,
                channel_id TEXT,
                channel_name TEXT,
                executor_id TEXT NOT NULL,
                executor_name TEXT NOT NULL,
                reason TEXT,
                removed_roles TEXT,
                status TEXT DEFAULT 'executed'
            )
        ''')
        conn.commit()
        conn.close()

    def _is_main_text_channel(self, channel) -> bool:
        # 仅监听主层文本频道，排除子区/线程
        return isinstance(channel, discord.TextChannel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            # 过滤：仅目标频道、指定Bot、主层文本频道
            if message.guild is None:
                return
            if not self._is_main_text_channel(message.channel):
                return
            if self.interface_channel_id and message.channel.id != self.interface_channel_id:
                return
            if self.interface_bot_id and message.author.id != self.interface_bot_id:
                return
            if self.bot.user and message.author.id == self.bot.user.id:
                return
            if not message.author.bot:
                return

            content = (message.content or "").strip()
            if not content:
                return

            # 严格JSON校验：仅 {"punish": <int>}
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                return

            if not isinstance(payload, dict):
                return
            if set(payload.keys()) != {"punish"}:
                return

            punish_value = payload.get("punish")
            if not isinstance(punish_value, int):
                return

            punished_user_id_str = str(punish_value)

            # 幂等：同一接口消息只处理一次
            if self._already_processed_interface_message(str(message.id)):
                return

            # 计算下一次处罚计数（以最近 executed 记录为准）
            next_count = self._compute_next_punish_count(punished_user_id_str)

            # 执行者信息：使用接口Bot（即消息作者）
            executor_id = str(self.interface_bot_id) if self.interface_bot_id else str(message.author.id)
            executor_name = getattr(message.author, "name", "同步")

            # 时间戳：采用接口消息时间
            ts = message.created_at.isoformat() if message.created_at else datetime.now().isoformat()

            # 写库
            self._insert_record(
                user_id=punished_user_id_str,
                user_name="不明",
                punish_count=next_count,
                timestamp=ts,
                original_message_id=str(message.id),   # 幂等锚点
                original_message_link=None,            # 原消息未知
                channel_id=None,                       # 原频道未知
                channel_name=None,                     # 原频道未知
                executor_id=executor_id,
                executor_name=executor_name,
                reason="同步",
                removed_roles_json="[]",               # 不恢复任何身份组
                status="executed"
            )

            print(f"[sync_punish] synced: user_id={punished_user_id_str}, count={next_count}, msg={message.id}")

        except Exception as e:
            print(f"[sync_punish] error: {e}")

    def _already_processed_interface_message(self, interface_msg_id: str) -> bool:
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM quick_punish_records WHERE original_message_id = ? AND status = 'executed' LIMIT 1",
                (interface_msg_id,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()

    def _compute_next_punish_count(self, user_id: str) -> int:
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT punish_count FROM quick_punish_records WHERE user_id = ? AND status = 'executed' ORDER BY timestamp DESC LIMIT 1",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                return 1
            try:
                last = int(row[0])
            except Exception:
                last = 0
            return max(1, last + 1)
        except Exception:
            return 1
        finally:
            conn.close()

    def _insert_record(
        self,
        user_id: str,
        user_name: str,
        punish_count: int,
        timestamp: str,
        original_message_id: Optional[str],
        original_message_link: Optional[str],
        channel_id: Optional[str],
        channel_name: Optional[str],
        executor_id: str,
        executor_name: str,
        reason: str,
        removed_roles_json: str,
        status: str
    ):
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        try:
            cursor.execute(
                '''
                INSERT INTO quick_punish_records
                (user_id, user_name, punish_count, timestamp, original_message_id, original_message_link,
                 channel_id, channel_name, executor_id, executor_name, reason, removed_roles, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    user_id,
                    user_name,
                    punish_count,
                    timestamp,
                    original_message_id,
                    original_message_link,
                    channel_id,
                    channel_name,
                    executor_id,
                    executor_name,
                    reason,
                    removed_roles_json,
                    status
                )
            )
            conn.commit()
        finally:
            conn.close()


async def setup(bot):
    await bot.add_cog(SyncPunishDataCog(bot))