import discord
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime
import asyncio
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 安全defer函数
async def safe_defer(interaction: discord.Interaction):
    """
    一个绝对安全的"占坑"函数。
    它会检查交互是否已被响应，如果没有，就立即以"仅自己可见"的方式延迟响应，
    这能完美解决超时和重复响应问题。
    """
    if not interaction.response.is_done():
        # ephemeral=True 让这个"占坑"行为对其他人不可见，不刷屏。
        await interaction.response.defer(ephemeral=True)

class GetContextCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    def _is_admin_or_kn_owner(self, user_id: int) -> tuple[bool, str]:
        """
        检查用户是否为admin或kn_owner
        返回: (是否有权限, 用户类型)
        """
        if hasattr(self.bot, 'admins') and user_id in self.bot.admins:
            return True, 'admin'
        if hasattr(self.bot, 'kn_owner') and user_id in self.bot.kn_owner:
            return True, 'kn_owner'
        return False, 'none'
    
    async def _get_thread_owner(self, thread: discord.Thread) -> int:
        """
        获取子区（线程）的创建者ID
        """
        try:
            # 获取线程的第一条消息（创建消息）
            async for message in thread.history(limit=1, oldest_first=True):
                return message.author.id
            # 如果没有消息，返回线程的owner_id
            return thread.owner_id if thread.owner_id else 0
        except Exception as e:
            logger.error(f"获取线程所有者失败: {e}")
            return 0
    
    def _parse_user_ids(self, user_ids_str: str) -> list[int]:
        """
        解析用户ID字符串，返回用户ID列表
        """
        if not user_ids_str or not user_ids_str.strip():
            return []
        
        user_ids = []
        for uid_str in user_ids_str.split(','):
            uid_str = uid_str.strip()
            if uid_str:
                # 检查是否为纯数字
                if uid_str.isdigit():
                    user_id = int(uid_str)
                    user_ids.append(user_id)
                else:
                    raise ValueError(f"用户ID必须为纯数字: {uid_str}")
        
        return user_ids
    
    def _validate_user_lists(self, whitelist: list[int], blacklist: list[int]) -> None:
        """
        验证白名单和黑名单，检查是否有重复的用户ID
        """
        if whitelist and blacklist:
            # 检查是否有用户ID同时出现在白名单和黑名单中
            overlap = set(whitelist) & set(blacklist)
            if overlap:
                overlap_ids = ', '.join(str(uid) for uid in overlap)
                raise ValueError(f"以下用户ID同时出现在白名单和黑名单中，请检查: {overlap_ids}")
    
    def _should_include_message(self, author_id: int, whitelist: list[int], blacklist: list[int]) -> bool:
        """
        根据白名单和黑名单判断是否应该包含该消息
        """
        # 如果有白名单，只包含白名单中的用户
        if whitelist:
            return author_id in whitelist
        
        # 如果有黑名单，排除黑名单中的用户
        if blacklist:
            return author_id not in blacklist
        
        # 如果都没有，包含所有用户
        return True
    
    async def _collect_messages(self, channel: discord.TextChannel | discord.Thread, 
                              whitelist: list[int] = None, blacklist: list[int] = None) -> list[dict]:
        """
        收集频道或线程中的所有消息
        返回消息列表，每条消息包含用户名和内容
        """
        if whitelist is None:
            whitelist = []
        if blacklist is None:
            blacklist = []
        
        messages = []
        message_count = 0
        filtered_count = 0
        
        try:
            # 分批获取消息，每次100条
            async for message in channel.history(limit=None):
                # 跳过没有文字内容的消息
                if not message.content.strip():
                    continue
                
                # 根据白名单和黑名单过滤消息
                if not self._should_include_message(message.author.id, whitelist, blacklist):
                    filtered_count += 1
                    continue
                
                # 记录消息
                messages.append({
                    'username': message.author.display_name,
                    'content': message.content,
                    'timestamp': message.created_at,
                    'author_id': message.author.id
                })
                
                message_count += 1
                
                # 每100条消息暂停5秒，避免API速率限制
                if message_count % 100 == 0:
                    logger.info(f"已收集 {message_count} 条消息，暂停5秒...")
                    await asyncio.sleep(5)
            
            # 按时间顺序排序（最早的在前）
            messages.sort(key=lambda x: x['timestamp'])
            
            logger.info(f"总共收集了 {len(messages)} 条有效消息，过滤了 {filtered_count} 条消息")
            return messages
            
        except discord.Forbidden:
            logger.error("没有权限访问该频道的消息历史")
            raise
        except discord.HTTPException as e:
            logger.error(f"Discord API错误: {e}")
            raise
        except Exception as e:
            logger.error(f"收集消息时发生未知错误: {e}")
            raise
    
    def _create_temp_file(self, messages: list[dict], user_id: int) -> str:
        """
        创建临时文件存储消息内容
        返回文件路径
        """
        # 确保context_temp文件夹存在
        temp_dir = 'context_temp'
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        
        # 生成文件名
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{user_id}_context.txt"
        filepath = os.path.join(temp_dir, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write("子区消息内容导出\n")
                f.write(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"总消息数: {len(messages)}\n")
                f.write("=" * 50 + "\n\n")
                
                for msg in messages:
                    f.write(f"{msg['username']}: {msg['content']}\n")
            
            logger.info(f"临时文件已创建: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"创建临时文件失败: {e}")
            raise
    
    async def _cleanup_file(self, filepath: str, delay: int = 300):
        """
        延迟删除临时文件（默认5分钟后删除）
        """
        try:
            await asyncio.sleep(delay)
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"临时文件已清理: {filepath}")
        except Exception as e:
            logger.error(f"清理临时文件失败: {e}")
    
    @app_commands.command(name='获取子区内容', description='[管理员/KN所有者] 获取子区内的所有消息内容')
    @app_commands.describe(
        whitelist='可选：白名单用户ID列表，多个ID用英文逗号分隔（仅获取这些用户的消息）',
        blacklist='可选：黑名单用户ID列表，多个ID用英文逗号分隔（排除这些用户的消息）'
    )
    async def get_context(self, interaction: discord.Interaction, 
                         whitelist: str = None, blacklist: str = None):
        """获取子区内容的斜杠命令"""
        # 永远先defer
        await safe_defer(interaction)
        
        try:
            # 检查权限
            has_permission, user_type = self._is_admin_or_kn_owner(interaction.user.id)
            if not has_permission:
                await interaction.followup.send(
                    "❌ 权限不足！此命令仅限管理员和KN所有者使用。",
                    ephemeral=True
                )
                return
            
            # 检查是否在线程中
            if not isinstance(interaction.channel, discord.Thread):
                await interaction.followup.send(
                    "❌ 此命令只能在子区（线程）中使用！",
                    ephemeral=True
                )
                return
            
            thread = interaction.channel
            
            # 如果是kn_owner，需要验证是否为该子区的所有者
            if user_type == 'kn_owner':
                thread_owner_id = await self._get_thread_owner(thread)
                if thread_owner_id != interaction.user.id:
                    await interaction.followup.send(
                        "❌ 权限不足！KN所有者只能获取自己创建的子区内容。",
                        ephemeral=True
                    )
                    return
            
            # 解析和验证白名单和黑名单
            try:
                whitelist_ids = self._parse_user_ids(whitelist) if whitelist else []
                blacklist_ids = self._parse_user_ids(blacklist) if blacklist else []
                
                # 验证白名单和黑名单是否有重复
                self._validate_user_lists(whitelist_ids, blacklist_ids)
                
            except ValueError as e:
                await interaction.followup.send(
                    f"❌ 参数错误：{str(e)}",
                    ephemeral=True
                )
                return
            
            # 构建过滤信息
            filter_info = []
            if whitelist_ids:
                filter_info.append(f"白名单用户: {len(whitelist_ids)} 个")
            if blacklist_ids:
                filter_info.append(f"黑名单用户: {len(blacklist_ids)} 个")
            
            filter_text = f" ({', '.join(filter_info)})" if filter_info else ""
            
            # 发送开始处理的消息
            await interaction.followup.send(
                f"🔄 开始收集子区消息{filter_text}，请稍候...",
                ephemeral=True
            )
            
            # 收集消息
            messages = await self._collect_messages(thread, whitelist_ids, blacklist_ids)
            
            if not messages:
                await interaction.followup.send(
                    "ℹ️ 该子区中没有找到任何文字消息。",
                    ephemeral=True
                )
                return
            
            # 创建临时文件
            filepath = self._create_temp_file(messages, interaction.user.id)
            
            # 发送文件
            with open(filepath, 'rb') as f:
                file = discord.File(f, filename=f"子区内容_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
                
                # 构建成功消息
                success_msg = f"✅ 成功收集了 {len(messages)} 条消息！\n"
                if filter_info:
                    success_msg += f"🔍 应用过滤条件: {', '.join(filter_info)}\n"
                success_msg += "📁 文件将在5分钟后自动删除。"
                
                await interaction.followup.send(
                    success_msg,
                    file=file,
                    ephemeral=True
                )
            
            # 异步清理文件
            asyncio.create_task(self._cleanup_file(filepath))
            
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 权限错误：无法访问该子区的消息历史。",
                ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Discord API错误：{str(e)}",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"获取子区内容时发生错误: {e}")
            await interaction.followup.send(
                "❌ 处理过程中发生错误，请稍后重试。",
                ephemeral=True
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(GetContextCog(bot))