import discord
from discord.ext import commands
from discord import app_commands
import os
import openai
import asyncio
import mimetypes
import base64
from datetime import datetime
import json
import random
from typing import Optional, List
from cogs.rag_processor import RAGProcessor
from PIL import Image
import io

# --- 从 bot.py 引入的辅助函数和类 ---

class QuotaError(app_commands.AppCommandError):
    """自定义异常，用于表示用户配额不足"""
    pass

class ParallelLimitError(app_commands.AppCommandError):
    """自定义异常，用于表示并发达到上限"""
    pass

def encode_image_to_base64(image_path):
    """将图片文件编码为Base64数据URI。"""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode('utf-8')
    return f"data:{mime_type};base64,{base64_encoded_data}"

# --- 安全的 defer 函数 ---
async def safe_defer(interaction: discord.Interaction):
    """
    一个绝对安全的"占坑"函数。
    它会检查交互是否已被响应，如果没有，就立即以"仅自己可见"的方式延迟响应，
    这能完美解决超时和重复响应问题。
    """
    if not interaction.response.is_done():
        # ephemeral=True 让这个"占坑"行为对其他人不可见，不刷屏。
        await interaction.response.defer(ephemeral=True)
        

# --- Cog 主体 ---

class AppDayi(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 消息冷却追踪器：存储 {message_id: last_used_timestamp}
        self.message_cooldowns = {}
        # 冷却时间（秒）
        self.cooldown_duration = 30
        
        # 初始化RAG处理器（如果启用）
        self.rag_processor = None
        if os.getenv("RAG_ENABLED", "false").lower() == "true":
            try:
                self.rag_processor = RAGProcessor()
                print("✅ RAG系统已启用并初始化")
            except Exception as e:
                print(f"⚠️ RAG系统初始化失败: {e}")
                self.rag_processor = None
        else:
            print("ℹ️ RAG系统未启用")
            
        # 将上下文菜单命令添加到 bot 的 tree 中
        self.ctx_menu = app_commands.ContextMenu(
            name='快速答疑',
            callback=self.quick_dayi,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        """Cog 卸载时移除命令"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
    
    def _get_file_size_kb(self, file_path: str) -> float:
        """
        获取文件大小（KB）
        
        Args:
            file_path: 文件路径
            
        Returns:
            文件大小（KB）
        """
        if os.path.exists(file_path):
            return os.path.getsize(file_path) / 1024
        return 0
    
    async def _compress_image(self, image_path: str, max_size_kb: int = 250) -> str:
        """
        压缩图片到指定大小以下
        
        Args:
            image_path: 原始图片路径
            max_size_kb: 最大文件大小（KB），默认250KB
            
        Returns:
            压缩后的图片路径（如果需要压缩）或原始路径
        """
        try:
            # 检查原始文件大小
            original_size_kb = self._get_file_size_kb(image_path)
            print(f"🖼️ 原始图片大小: {original_size_kb:.2f}KB")
            
            # 如果小于限制，直接返回
            if original_size_kb <= max_size_kb:
                print("✅ 图片大小符合要求，无需压缩")
                return image_path
            
            # 需要压缩
            print(f"🔧 开始压缩图片 (目标: <{max_size_kb}KB)")
            
            # 打开图片
            with Image.open(image_path) as img:
                # 转换为RGB（如果是RGBA或其他格式）
                if img.mode in ('RGBA', 'LA', 'P'):
                    # 创建白色背景
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'RGBA' or img.mode == 'LA':
                        background.paste(img, mask=img.split()[-1])
                    else:
                        background.paste(img)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # 生成压缩后的文件路径
                base_name = os.path.splitext(image_path)[0]
                compressed_path = f"{base_name}_compressed.jpg"
                
                # 初始参数
                quality = 85
                max_dimension = 1920
                
                # 循环压缩直到满足大小要求
                for attempt in range(5):  # 最多尝试5次
                    # 调整尺寸
                    width, height = img.size
                    if width > max_dimension or height > max_dimension:
                        ratio = min(max_dimension / width, max_dimension / height)
                        new_width = int(width * ratio)
                        new_height = int(height * ratio)
                        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                        print(f"  调整尺寸: {width}x{height} → {new_width}x{new_height}")
                    else:
                        resized_img = img
                    
                    # 保存到内存缓冲区以检查大小
                    buffer = io.BytesIO()
                    resized_img.save(buffer, format='JPEG', quality=quality, optimize=True)
                    buffer_size_kb = buffer.tell() / 1024
                    
                    print(f"  尝试 {attempt + 1}: 质量={quality}, 大小={buffer_size_kb:.2f}KB")
                    
                    # 如果满足要求，保存到文件
                    if buffer_size_kb <= max_size_kb:
                        buffer.seek(0)
                        with open(compressed_path, 'wb') as f:
                            f.write(buffer.read())
                        print(f"✅ 压缩成功: {original_size_kb:.2f}KB → {buffer_size_kb:.2f}KB")
                        print(f"   压缩率: {(1 - buffer_size_kb/original_size_kb) * 100:.1f}%")
                        return compressed_path
                    
                    # 调整参数继续尝试
                    if attempt < 2:
                        quality -= 10  # 降低质量
                    else:
                        max_dimension = int(max_dimension * 0.8)  # 缩小尺寸
                        quality = 75  # 重置质量
                
                # 如果仍然无法满足要求，使用最后的尝试结果
                print(f"⚠️ 无法压缩到{max_size_kb}KB以下，使用最佳尝试结果")
                buffer.seek(0)
                with open(compressed_path, 'wb') as f:
                    f.write(buffer.read())
                return compressed_path
                
        except Exception as e:
            print(f"❌ 图片压缩失败: {e}")
            # 压缩失败时返回原始路径
            return image_path
    
    async def _describe_image(self, image_path: str) -> str:
        """
        使用图片描述模型生成图片的文本描述
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            图片的文本描述
        """
        try:
            # 系统提示词
            system_prompt = """你是专业图片描述助手。请详细描述图片中的内容，包括：
- 主要对象
- 文字内容（如果有，请完整准确地提取，包括文字的颜色等）
- 技术细节（如代码、图表、UI界面、错误信息等）

用简洁准确的中文描述，重点关注可能与技术问题相关的内容。"""
            
            # 编码图片
            base64_image = encode_image_to_base64(image_path)
            
            # 构建请求
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": base64_image}}
                ]}
            ]
            
            # 调用API（使用IMAGE_DESCRIBE_MODEL）
            client = self.bot.openai_client
            loop = asyncio.get_event_loop()
            
            # 设置较短的超时时间（30秒）
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: client.chat.completions.create(
                        model=os.getenv("IMAGE_DESCRIBE_MODEL", "gemini-2.5-flash-lite-preview-06-17"),
                        messages=messages,
                        temperature=0.3,  # 较低的温度以获得更准确的描述
                        max_tokens=600
                    )
                ),
                timeout=30.0
            )
            
            # 🔧 修复：处理异步调用可能返回列表的问题
            if isinstance(response, list):
                print("⚠️ [图片描述] 检测到列表响应，尝试提取第一个元素")
                if response and len(response) > 0:
                    # 检查第一个元素是否有 choices 属性
                    if hasattr(response[0], 'choices'):
                        response = response[0]
                        print("✅ [图片描述] 成功从列表中提取响应对象")
                    else:
                        # 可能整个列表就是 choices
                        print("⚠️ [图片描述] 列表可能就是 choices，尝试直接使用")
                        # 创建一个模拟的响应对象
                        class MockResponse:
                            def __init__(self, choices):
                                self.choices = choices
                        response = MockResponse(response)
                else:
                    print("❌ [图片描述] 错误：返回了空列表")
                    return "图片描述失败: API返回了空列表"
            
            # 检查 choices 是否存在
            if not hasattr(response, 'choices'):
                print("❌ [图片描述] 错误：response 没有 choices 属性")
                print(f"   response 类型: {type(response)}")
                return "图片描述失败: API响应格式错误"
            
            # 检查 choices 是否为空
            if not response.choices or len(response.choices) == 0:
                print("❌ [图片描述] 错误：choices 为空")
                return "图片描述失败: API未返回结果"
                
            description = response.choices[0].message.content
            print(f"🖼️ 图片描述成功，长度: {len(description)}")
            return description
            
        except asyncio.TimeoutError:
            print("⚠️ 图片描述超时（30秒）")
            return "图片描述超时"
        except Exception as e:
            print(f"❌ 图片描述失败: {e}")
            return f"图片描述失败: {str(e)}"
    
    async def _parallel_rag_retrieve_multiple_images(self, text: str, image_paths: List[str], compressed_paths: List[str] = None) -> List[dict]:
        """
        并行执行文本和多张图片的RAG检索
        
        Args:
            text: 文本内容
            image_paths: 图片文件路径列表（用于描述）
            compressed_paths: 压缩后的图片路径列表（可选，用于API调用）
            
        Returns:
            合并并去重后的检索结果
        """
        tasks = []
        task_types = []
        
        # 如果没有提供压缩路径，使用原始路径
        if compressed_paths is None:
            compressed_paths = image_paths
        
        # 任务1：文本RAG检索
        if text:
            print("📝 启动文本RAG检索任务")
            tasks.append(self.rag_processor.retrieve_context(text))
            task_types.append("text")
        
        # 任务2-N：每张图片独立的描述 + RAG检索
        # 注意：这里使用压缩后的图片进行描述，以保证一致性
        for idx, img_path in enumerate(compressed_paths):
            if img_path and os.path.exists(img_path):
                async def image_to_rag(img_path, img_idx):
                    try:
                        print(f"🖼️ 启动图片 {img_idx+1}/{len(compressed_paths)} 描述任务")
                        # 获取图片描述
                        description = await self._describe_image(img_path)
                        if description and description not in ["图片描述超时", "图片描述失败"]:
                            print(f"📝 使用图片 {img_idx+1} 的描述进行RAG检索")
                            # 使用描述进行RAG检索
                            return await self.rag_processor.retrieve_context(description)
                        else:
                            print(f"⚠️ 图片 {img_idx+1} 描述无效，跳过RAG检索")
                            return []
                    except Exception as e:
                        print(f"❌ 图片 {img_idx+1} RAG检索失败: {e}")
                        return []
                
                tasks.append(image_to_rag(img_path, idx))
                task_types.append(f"image_{idx+1}")
        
        # 如果没有任务，返回空结果
        if not tasks:
            return []
        
        # 并行执行所有任务
        print(f"⏳ 并行执行 {len(tasks)} 个RAG检索任务...")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 收集所有检索结果
        all_contexts = []
        
        for i, (result, task_type) in enumerate(zip(results, task_types)):
            if isinstance(result, Exception):
                print(f"❌ {task_type} 任务失败: {result}")
                continue
            
            if result:
                all_contexts.extend(result)
                print(f"✅ {task_type} 检索到 {len(result)} 个文档块")
        
        # 去重和排序
        seen_texts = set()
        unique_contexts = []
        for ctx in sorted(all_contexts, key=lambda x: x.get('similarity', 0), reverse=True):
            # 用前200字符作为去重标识
            ctx_text = ctx['text'][:200] if 'text' in ctx else str(ctx)[:200]
            if ctx_text not in seen_texts:
                unique_contexts.append(ctx)
                seen_texts.add(ctx_text)
                # 限制最大文档数
                if len(unique_contexts) >= self.rag_processor.top_k:
                    break
        
        print(f"✅ 合并去重后得到 {len(unique_contexts)} 个文档块")
        return unique_contexts
    
    async def _parallel_rag_retrieve(self, text: str, image_data: Optional[bytes] = None, image_path: Optional[str] = None) -> tuple:
        """
        并行执行文本和图片的RAG检索（保留用于兼容性）
        
        Args:
            text: 文本内容
            image_data: 图片字节数据
            image_path: 图片文件路径
            
        Returns:
            (text_contexts, image_contexts) - 分别来自文本和图片描述的检索结果
        """
        if image_path:
            contexts = await self._parallel_rag_retrieve_multiple_images(text, [image_path])
            # 简单地将结果分成两部分返回（为了兼容）
            return contexts[:len(contexts)//2], contexts[len(contexts)//2:]
        else:
            contexts = await self._parallel_rag_retrieve_multiple_images(text, [])
            return contexts, []
    
    def _clean_expired_cooldowns(self):
        """清理过期的冷却记录"""
        current_time = datetime.now()
        expired_messages = [
            msg_id for msg_id, last_used in self.message_cooldowns.items()
            if (current_time - last_used).total_seconds() > self.cooldown_duration
        ]
        for msg_id in expired_messages:
            del self.message_cooldowns[msg_id]
    
    def _check_and_update_cooldown(self, message_id: int) -> tuple[bool, int]:
        """
        检查消息是否在冷却中，如果不在则更新冷却时间
        
        Returns:
            (is_on_cooldown, remaining_seconds) - 如果在冷却中返回(True, 剩余秒数)，否则返回(False, 0)
        """
        # 先清理过期的记录（防止内存无限增长）
        self._clean_expired_cooldowns()
        
        current_time = datetime.now()
        
        # 检查该消息是否在冷却中
        if message_id in self.message_cooldowns:
            last_used = self.message_cooldowns[message_id]
            elapsed = (current_time - last_used).total_seconds()
            
            if elapsed < self.cooldown_duration:
                # 仍在冷却中
                remaining = int(self.cooldown_duration - elapsed)
                return True, remaining
        
        # 不在冷却中，更新时间戳
        self.message_cooldowns[message_id] = current_time
        return False, 0

    async def quick_dayi(self, interaction: discord.Interaction, message: discord.Message):
        """
        对消息使用 /dayi 功能。
        提取消息中的文本和图片，调用 OpenAI API，并将结果公开回复。
        """
        
        
        # 🔥 黄金法则：永远先 defer！
        await safe_defer(interaction)
        
        user_id = interaction.user.id
        
        # --- 封禁检查 ---
        # 检查被引用消息的作者是否被封禁
        target_user = message.author
        target_user_id = str(target_user.id)  # 转换为字符串以匹配JSON格式
        
        # 从 banlist.json 加载封禁列表
        try:
            banlist_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'banlist.json')
            with open(banlist_path, 'r', encoding='utf-8') as f:
                banlist_data = json.load(f)
                
            # 检查用户是否在封禁列表中
            banned_user_info = None
            current_timestamp = datetime.now().timestamp()
            
            for ban_entry in banlist_data.get('banlist', []):
                if ban_entry['ID'] == target_user_id:
                    # 检查是否已经解封
                    unbanned_at = int(ban_entry['unbanned_at'])
                    if current_timestamp < unbanned_at:
                        banned_user_info = ban_entry
                        break
            
            if banned_user_info:
                # 格式化解封时间
                unbanned_timestamp = int(banned_user_info['unbanned_at'])
                unbanned_date = datetime.fromtimestamp(unbanned_timestamp)
                formatted_date = unbanned_date.strftime('%Y年%m月%d日 %H:%M:%S')
                
                # 构建封禁信息消息
                ban_message = (
                    f"❌ **该用户已被开发者封禁**\n\n"
                    f"**用户ID:** {banned_user_info['ID']}\n"
                    f"**封禁原因:** {banned_user_info['reason']}\n"
                    f"**解封时间:** {formatted_date}"
                )
                
                # 在频道公开发送封禁消息（不使用embed）
                await interaction.channel.send(ban_message)
                
                # 编辑原始响应（私有消息）
                await interaction.edit_original_response(content="❌ 该用户已被封禁，无法对其使用快速答疑功能。")
                
                print(f"🚫 尝试对封禁用户 {target_user_id} ({target_user.name}) 的消息使用快速答疑")
                print(f"   封禁原因: {banned_user_info['reason']}")
                print(f"   解封时间: {formatted_date}")
                return
                
            # 调试日志
            print(f"✅ 用户 {target_user_id} ({target_user.name}) 未被封禁")
            
        except FileNotFoundError:
            print("⚠️ banlist.json 文件不存在，跳过封禁检查")
        except json.JSONDecodeError as e:
            print(f"❌ 解析 banlist.json 失败: {e}")
        except Exception as e:
            print(f"❌ 封禁检查出错: {e}")
            
        # --- 权限检查 ---
        if not (user_id in self.bot.admins or user_id in self.bot.trusted_users):
            
            await interaction.edit_original_response(content='❌ 没权。此命令仅限答疑组使用。')
            return
        
        # --- 冷却检查 ---
        is_on_cooldown, remaining_seconds = self._check_and_update_cooldown(message.id)
        if is_on_cooldown:
            
            await interaction.edit_original_response(
                content=f'⏰ 该消息正在冷却中，请在 **{remaining_seconds}** 秒后再试。\n'
                f'（每条消息在使用快速答疑后需要等待 {self.cooldown_duration} 秒才能再次使用）'
            )
            return
        
        # --- 并发检查 ---
        # 注意：这里我们假设 bot 实例上有一个 current_parallel_dayi_tasks 属性
        if not hasattr(self.bot, 'current_parallel_dayi_tasks'):
            self.bot.current_parallel_dayi_tasks = 0
        
        max_parallel = int(os.getenv("MAX_PARALLEL", 5))
        if self.bot.current_parallel_dayi_tasks >= max_parallel:
            
            await interaction.edit_original_response(content=f"❌ 当前并发数已达上限 ({max_parallel})，请稍后再试。")
            return

        # 更新状态消息
        
        await interaction.edit_original_response(content="⏳ 收到请求，正在处理中，请稍候...")
        

        # --- 文件处理 ---
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_filename = f"{timestamp}_{user_id}"
        temp_dir = 'app_temp'
        image_paths = []
        image_data_list = []
        text_path = None
        
        # 提取消息文本
        text = message.content if message.content else "这是什么问题，怎么解决"
        
        # 提取消息中的所有图片附件
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        
        # 🔍 调试：记录图片附件信息
        if image_attachments:
            print(f"📸 [快速答疑] 检测到 {len(image_attachments)} 张图片附件")
            for idx, att in enumerate(image_attachments):
                print(f"   图片{idx+1}: {att.filename} ({att.size / 1024:.2f} KB)")
        
        # 检查图片数量限制
        if len(image_attachments) > 3:
            print(f"⚠️ [快速答疑] 图片数量超限: {len(image_attachments)} > 3")
            
            # 使用 edit_original_response 更新已经 defer 的响应
            await interaction.edit_original_response(
                content=f'❌ 图片数量超出限制！\n'
                f'当前消息包含 **{len(image_attachments)}** 张图片，系统最多支持 **3** 张图片。\n'
                f'请减少图片数量后重试。'
            )
            
            return

        try:
            self.bot.current_parallel_dayi_tasks += 1
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)

            # 保存文本
            text_path = os.path.join(temp_dir, f"{base_filename}.txt")
            with open(text_path, 'w', encoding='utf-8') as f:
                f.write(text)

            # 保存所有图片
            for idx, image_attachment in enumerate(image_attachments):
                _, image_extension = os.path.splitext(image_attachment.filename)
                image_path = os.path.join(temp_dir, f"{base_filename}_{idx}{image_extension}")
                await image_attachment.save(image_path)
                image_paths.append(image_path)
                # 同时读取图片数据用于多模态RAG（如果需要）
                with open(image_path, 'rb') as f:
                    image_data_list.append(f.read())
            
            if image_attachments:
                print(f"📸 保存了 {len(image_attachments)} 张图片")
        
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ 处理文件时出错: {e}")
            print(f" [31m[错误] [0m 用户 {user_id} 在 '快速答疑' 中保存文件时失败: {e}")
            self.bot.current_parallel_dayi_tasks -= 1
            return

        # --- OpenAI 请求 ---
        try:
            # 创建并行任务组
            parallel_tasks = {}
            compressed_paths = image_paths  # 默认使用原始路径
            
            # 如果有图片，创建压缩任务
            if image_paths:
                print("🚀 开始并行处理：图片压缩 + RAG检索...")
                parallel_tasks['compress'] = asyncio.gather(
                    *[self._compress_image(path) for path in image_paths]
                )
            
            # 根据是否启用RAG系统选择不同的提示词构建方式
            if self.rag_processor:
                # 使用RAG系统检索相关内容
                try:
                    contexts = []
                    
                    # 判断是否有图片
                    if image_paths:
                        # 先等待压缩完成，然后使用压缩后的图片进行描述和RAG
                        if 'compress' in parallel_tasks:
                            compressed_paths = await parallel_tasks['compress']
                            print("✅ 图片压缩完成")
                        
                        # 新流程：并行处理文本和多张图片（使用压缩后的图片）
                        print(f"🚀 开始并行RAG检索 - 文本长度: {len(text)}, 图片数量: {len(compressed_paths)}")
                        contexts = await self._parallel_rag_retrieve_multiple_images(
                            text=text,
                            image_paths=image_paths,
                            compressed_paths=compressed_paths
                        )
                    else:
                        # 纯文本：保持原流程
                        print(f"📝 开始纯文本检索 - 文本长度: {len(text)}")
                        contexts = await self.rag_processor.retrieve_context(text)
                        print(f"✅ RAG文本检索到 {len(contexts)} 个相关文档块")
                    
                    if contexts:
                        # 构建增强的系统提示词
                        system_prompt = await self.rag_processor.build_enhanced_prompt(
                            text,  # 始终使用文本构建提示词
                            contexts
                        )
                    else:
                        # 如果没有检索到相关内容，使用默认提示词
                        print("⚠️ RAG未检索到相关内容，使用默认提示词")
                        system_prompt = self._load_default_prompt()
                except Exception as e:
                    print(f"❌ RAG检索失败: {e}，回退到默认提示词")
                    import traceback
                    traceback.print_exc()
                    system_prompt = self._load_default_prompt()
            else:
                # RAG未启用，使用传统方式加载整个知识库
                system_prompt = self._load_default_prompt()
            
            # 如果还没有执行压缩，现在执行（处理没有RAG的情况）
            if image_paths and 'compress' in parallel_tasks and compressed_paths == image_paths:
                compressed_paths = await parallel_tasks['compress']
                print("✅ 图片压缩完成")
            
            # 使用压缩后的路径替换原始路径
            if compressed_paths != image_paths:
                image_paths = compressed_paths
            
            # 构建请求内容
            user_content = [{"type": "text", "text": text}]
            # 添加所有图片到请求中（使用压缩后的图片）
            for image_path in image_paths:
                # 打印每个图片的最终大小
                size_kb = self._get_file_size_kb(image_path)
                print(f"📎 添加图片到API请求: {os.path.basename(image_path)} ({size_kb:.2f}KB)")
                
                base64_image = encode_image_to_base64(image_path)
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": base64_image}
                })
            
            # 计算总大小
            if image_paths:
                total_size_kb = sum(self._get_file_size_kb(path) for path in image_paths)
                print(f"📊 API请求图片总大小: {total_size_kb:.2f}KB")

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]
            
            # 存档完整提示词到app_save文件夹
            try:
                # 确保app_save文件夹存在
                save_dir = "app_save"
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)
                
                # 创建存档文件名
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                save_filename = f"{timestamp}_{user_id}.txt"
                save_path = os.path.join(save_dir, save_filename)
                
                # 保存提示词
                with open(save_path, "w", encoding="utf-8") as f:
                    # 保存系统提示词
                    f.write("=== 系统提示词 ===\n")
                    f.write(system_prompt)
                    f.write("\n\n=== 用户提问 ===\n")
                    f.write(text)
                    if image_paths:
                        f.write(f"\n[包含 {len(image_paths)} 张图片附件]\n")
                
                print(f"✅ 已存档提示词到 {save_path}")
            except Exception as e:
                print(f"❌ 存档提示词失败: {e}")

            client = self.bot.openai_client # 假设 client 在 bot 实例上
            
            # 🔍 添加调试信息：记录请求详情
            print("📤 [API请求] 准备发送请求:")
            print(f"   - 模型: {os.getenv('OPENAI_MODEL')}")
            print(f"   - 文本长度: {len(text)} 字符")
            print(f"   - 图片数量: {len(image_paths)} 张")
            if image_paths:
                print(f"   - 图片总大小: {sum(self._get_file_size_kb(p) for p in image_paths):.2f} KB")
            
            # 异步执行API请求，设置3分钟超时
            loop = asyncio.get_event_loop()
            try:
                # 使用 asyncio.wait_for 设置180秒（3分钟）超时
                # 🔧 特殊处理：某些API在处理多图片时可能需要特殊处理
                # 先尝试正常调用
                raw_response = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: client.chat.completions.create(
                            model=os.getenv("OPENAI_MODEL"),
                            messages=messages,
                            temperature=1.0,
                            stream=False
                        )
                    ),
                    timeout=180.0  # 3分钟超时
                )
                
                # 保存原始响应用于调试
                response = raw_response
                
                # 🔍 详细记录响应类型和结构
                print("📥 [API响应] 收到响应:")
                print(f"   - 类型: {type(response)}")
                print(f"   - 是否为列表: {isinstance(response, list)}")
                
                # 🔧 特殊处理：处理可能的列表响应（多图片时某些API的特殊行为）
                if isinstance(response, list):
                    print(f"⚠️ [主API] 检测到列表响应（多图片场景），长度: {len(response)}")
                    
                    # 🆕 处理策略：对于多图片返回的列表，尝试多种解析方式
                    if response and len(response) > 0:
                        # 详细记录列表内容
                        print(f"   - 第一个元素类型: {type(response[0])}")
                        
                        # 策略1：检查是否每个元素都是完整的ChatCompletion对象
                        if all(hasattr(item, 'choices') for item in response if item is not None):
                            print("   ✅ 列表中所有元素都有choices属性")
                            # 合并所有响应的内容
                            combined_content = []
                            for idx, item in enumerate(response):
                                if item and item.choices and len(item.choices) > 0:
                                    content = item.choices[0].message.content
                                    if content:
                                        combined_content.append(content)
                                        print(f"   - 响应{idx+1}内容长度: {len(content)}")
                            
                            if combined_content:
                                # 创建合并后的响应
                                print(f"   ✅ 合并{len(combined_content)}个响应")
                                # 使用第一个响应作为基础，替换内容
                                response = response[0]
                                if response.choices and response.choices[0].message:
                                    response.choices[0].message.content = "\n\n".join(combined_content)
                            else:
                                # 使用第一个有效响应
                                for idx, item in enumerate(response):
                                    if item and item.choices and len(item.choices) > 0:
                                        response = item
                                        print(f"   ✅ 使用列表中的第{idx+1}个响应")
                                        break
                        
                        # 策略2：第一个元素有choices属性
                        elif hasattr(response[0], 'choices'):
                            response = response[0]
                            print("   ✅ [主API] 从列表中提取第一个响应对象")
                        
                        # 策略3：列表本身可能就是choices
                        else:
                            print("   ⚠️ [主API] 列表元素没有choices属性")
                            # 检查列表元素是否像choice对象
                            if hasattr(response[0], 'message'):
                                print("   - 检测到message属性，尝试构造响应")
                                # 创建模拟的响应对象
                                class MockResponse:
                                    def __init__(self, choices):
                                        self.choices = choices
                                        self.model = os.getenv("OPENAI_MODEL")
                                        self.created = None
                                response = MockResponse(response)
                            else:
                                print("   ❌ 无法解析列表响应格式")
                                await interaction.edit_original_response(
                                    content="❌ **API响应格式异常**\n"
                                           f"当处理{len(image_attachments)}张图片时，API返回了无法解析的格式。\n\n"
                                           "**建议：**\n"
                                           "• 尝试减少图片数量（建议1-2张）\n"
                                           "• 确保图片格式正确（JPG/PNG）\n"
                                           "• 如问题持续，请联系管理员"
                                )
                                return
                    else:
                        print("   ❌ API返回了空列表")
                        await interaction.edit_original_response(
                            content="❌ **API返回空列表**\n"
                                   f"处理{len(image_attachments)}张图片时出现问题。\n\n"
                                   "**可能的原因：**\n"
                                   "• 图片总大小超出限制\n"
                                   "• API暂时无法处理多图片\n"
                                   "• 请尝试减少图片数量或大小"
                        )
                        return
                
                # 记录最终响应信息
                if hasattr(response, 'choices'):
                    print("   ✅ 最终响应有choices属性")
                    print(f"   - choices数量: {len(response.choices) if response.choices else 0}")
                
                # 检查响应对象属性
                if not hasattr(response, 'choices'):
                    print("❌ [主API] 错误：response 没有 choices 属性")
                    print(f"   - response 类型: {type(response)}")
                    print(f"   - response 内容预览: {str(response)[:500]}")
                    await interaction.edit_original_response(
                        content="❌ **API响应格式错误**：响应对象缺少必要属性\n"
                                "可能的原因：\n"
                                "• 多图片处理时API返回格式异常\n"
                                "• 请尝试减少图片数量\n"
                                "• 或联系管理员检查API配置"
                    )
                    return
                
                # 检查空响应
                if not response or not response.choices or len(response.choices) == 0:
                    error_msg = "API返回空响应：没有choices数据"
                    print(f"❌ {error_msg}")
                    print(f"   - 图片数量: {len(image_paths)}")
                    print(f"   - 文本长度: {len(text)}")
                    
                    # 根据图片数量提供更具体的错误信息
                    if len(image_paths) > 1:
                        await interaction.edit_original_response(
                            content=f"❌ **多图片处理失败**\n\n"
                                    f"当前发送了 **{len(image_paths)}** 张图片，API无法正常处理。\n\n"
                                    "**解决方案：**\n"
                                    "• 减少图片数量（建议每次1-2张）\n"
                                    "• 降低图片分辨率\n"
                                    "• 分多次提问\n\n"
                                    "💡 **提示：**某些API模型对多图片支持有限，建议使用单张图片获得最佳效果。"
                        )
                    else:
                        await interaction.edit_original_response(
                            content=f"❌ **{error_msg}**\n\n"
                                    "可能的原因：\n"
                                    "• API服务暂时不可用\n"
                                    "• 图片格式不受支持\n"
                                    "• 网络连接问题\n\n"
                                    "请稍后重试或联系管理员。"
                        )
                    return
                
                # 检查 message.content 是否存在
                if not response.choices[0].message or response.choices[0].message.content is None:
                    error_msg = "API返回空响应：content为空"
                    print(f"❌ {error_msg}")
                    await interaction.edit_original_response(
                        content=f"❌ **{error_msg}**\n"
                               "可能的原因：\n"
                               "• 内容被过滤\n"
                               "• 模型无法生成响应\n"
                               "• 请尝试修改问题后重试"
                    )
                    return
                
                ai_response = response.choices[0].message.content
            except asyncio.TimeoutError:
                # 超时处理
                await interaction.edit_original_response(
                    content="⏱️ **答疑超时**：处理时间超过3分钟，请求已被终止。\n"
                           "建议：\n"
                           "• 简化问题描述\n"
                           "• 减小图片尺寸\n"
                           "• 稍后重试"
                )
                print(f"⚠️ [超时] 用户 {user_id} 的快速答疑请求超过3分钟被终止")
                return

            # --- 公开回复 ---
            # 获取随机模型名称
            random_model_names = os.getenv('RANDOM_MODEL_NAMES', '')
            if random_model_names:
                # 将逗号分隔的名称列表转换为数组
                model_names = [name.strip() for name in random_model_names.split(',') if name.strip()]
                if model_names:
                    # 随机选择一个模型名称
                    display_model_name = random.choice(model_names)
                else:
                    # 如果列表为空，使用原始模型名称
                    display_model_name = os.getenv('OPENAI_MODEL')
            else:
                # 如果环境变量未设置，使用原始模型名称
                display_model_name = os.getenv('OPENAI_MODEL')
            
            # 创建回复内容的 Embed
            embed = discord.Embed(
                title="🦊 AI 回复",
                description=ai_response,
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"由 {display_model_name} 提供支持 | {interaction.user.display_name} 问的。")
            
            # 回复原始消息
            await message.reply(embed=embed)
            
            # 编辑初始的临时消息，提示操作完成
            await interaction.edit_original_response(content="✅ 已成功回复。")

        except asyncio.TimeoutError:
            # 这个异常已经在上面的 try-except 中处理了
            pass
        except openai.APIConnectionError as e:
            await interaction.edit_original_response(content=f"❌ **连接错误**: 无法连接到AI服务。\n`{e}`")
        except openai.RateLimitError as e:
            await interaction.edit_original_response(content=f"❌ **请求超速**: 已达到API的请求频率限制。\n`{e}`")
        except openai.AuthenticationError as e:
            await interaction.edit_original_response(content=f"❌ **认证失败**: API密钥无效或已过期。\n`{e}`")
        except openai.APIStatusError as e:
            await interaction.edit_original_response(content=f"❌ **API 错误**: API返回了非200的状态码。\n状态码: {e.status_code}\n响应: {e.response}")
        except json.JSONDecodeError as e:
            # 专门处理JSON解析错误（通常是空响应导致）
            error_msg = f"API返回空响应：Expecting value: line {e.lineno} column {e.colno} (char {e.pos})"
            print(f"❌ {error_msg}")
            await interaction.edit_original_response(
                content=f"❌ **{error_msg}**\n"
                       "可能的原因：\n"
                       "• API返回了空的或无效的JSON\n"
                       "• 网络传输中断\n"
                       "• 请稍后重试"
            )
        except Exception as e:
            # 检查是否是特定的"Expecting value"错误
            error_str = str(e)
            
            # 🔍 调试：打印完整的错误信息和堆栈
            import traceback
            print(f"❌ [快速答疑] 捕获异常: {type(e).__name__}: {e}")
            print(f"   错误详情: {error_str}")
            print("   堆栈跟踪:")
            traceback.print_exc()
            
            # 检查是否是 'list' object has no attribute 'choices' 错误
            if "'list' object has no attribute 'choices'" in error_str:
                print("🔍 诊断：检测到列表访问choices属性错误")
                await interaction.edit_original_response(
                    content="❌ **API响应格式错误**\n"
                           "错误: 'list' object has no attribute 'choices'\n\n"
                           "**可能的原因：**\n"
                           "• API返回了意外的列表格式\n"
                           "• API配置可能有问题\n"
                           "• 请联系管理员检查API设置"
                )
            elif "Expecting value: line 1 column 1 (char 0)" in error_str:
                error_msg = f"API返回空响应：{error_str}"
                print(f"❌ {error_msg}")
                await interaction.edit_original_response(
                    content=f"❌ **{error_msg}**\n"
                           "可能的原因：\n"
                           "• API返回了完全空的响应\n"
                           "• 服务端处理异常\n"
                           "• 请稍后重试"
                )
            else:
                print(f" [31m[AI错误] [0m '快速答疑' 调用AI时发生错误: {e}")
                await interaction.edit_original_response(content=f"❌ 发生意外错误: {e}，请联系管理员。")
        
        finally:
            self.bot.current_parallel_dayi_tasks -= 1
            # 清理临时文件
            if os.getenv("DELETE_TEMP_FILES", "false").lower() == "true":
                # 清理文本文件
                if text_path and os.path.exists(text_path):
                    try:
                        os.remove(text_path)
                        print(f"🗑️ 已删除临时文件: {os.path.basename(text_path)}")
                    except Exception as e:
                        print(f" [33m[警告] [0m 删除临时文件 {text_path} 时出错: {e}")
                
                # 收集所有需要清理的图片文件（包括原始和压缩的）
                all_image_paths = set()  # 使用set避免重复
                
                # 添加当前使用的图片路径（可能是压缩后的）
                for path in image_paths:
                    if path:
                        all_image_paths.add(path)
                
                # 添加原始图片路径（以防压缩后的路径不同）
                for idx, _ in enumerate(image_attachments):
                    _, image_extension = os.path.splitext(image_attachments[idx].filename)
                    original_path = os.path.join(temp_dir, f"{base_filename}_{idx}{image_extension}")
                    all_image_paths.add(original_path)
                    # 添加可能的压缩文件路径
                    compressed_path = f"{os.path.splitext(original_path)[0]}_compressed.jpg"
                    all_image_paths.add(compressed_path)
                
                # 清理所有图片文件
                for image_path in all_image_paths:
                    if image_path and os.path.exists(image_path):
                        try:
                            os.remove(image_path)
                            print(f"🗑️ 已删除临时文件: {os.path.basename(image_path)}")
                        except Exception as e:
                            print(f" [33m[警告] [0m 删除临时文件 {image_path} 时出错: {e}")

    def _load_default_prompt(self):
        """加载默认的完整知识库提示词"""
        prompt_file = "prompt/ALL.txt"
        try:
            with open(prompt_file, 'r', encoding='utf-8') as f:
                system_prompt = f.read().strip()
            if not system_prompt:
                system_prompt = "You are a helpful assistant."
            print("📖 使用完整知识库作为提示词")
            return system_prompt
        except FileNotFoundError:
            print("⚠️ 知识库文件不存在，使用默认提示词")
            return "You are a helpful assistant."

async def setup(bot: commands.Bot):
    # 在 setup 函数中传递 bot 实例
    # 确保 bot.py 中的 client 被设置为 bot 的属性
    if not hasattr(bot, 'openai_client'):
         # 从 .env 文件加载配置
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        OPENAI_API_BASE_URL = os.getenv("OPENAI_API_BASE_URL")
        if not all([OPENAI_API_KEY, OPENAI_API_BASE_URL]):
            print(" [错误](来自App) 缺少必要的 OpenAI 环境变量。")
            bot.openai_client = None
        else:
            bot.openai_client = openai.OpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_API_BASE_URL,
            )

    await bot.add_cog(AppDayi(bot))