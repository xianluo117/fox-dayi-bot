"""  
RAG 智能索引器模块
负责知识库文档的智能分块
"""

import re
from typing import List, Dict, Optional, Tuple
import tiktoken
from discord.ext import commands

class RAGIndexer:
    """
    RAG智能索引器，负责将文档进行语义化、结构化的智能分块。
    """
    
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        """
        初始化索引器
        
        Args:
            chunk_size: 目标块大小 (tokens)
            chunk_overlap: 块之间的重叠大小 (tokens)
        """
        self.target_chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.encoding = tiktoken.get_encoding("cl100k_base")

    def _count_tokens(self, text: str) -> int:
        """计算文本的token数量"""
        return len(self.encoding.encode(text, disallowed_special=()))

    def smart_split(self, text: str, metadata: Optional[Dict] = None) -> List[Dict]:
        """
        智能分块主方法 - 采用两级分割策略
        """
        # 1. 预处理：清理文本
        text = self._preprocess_text(text)
        
        # 2. 第一级分割：按 '===' 分割成主要部分
        major_sections = re.split(r'\n\s*===\s*\n', text)
        
        all_sub_sections = []
        for section in major_sections:
            section = section.strip()
            if not section:
                continue
            
            # 提取该主要部分的顶级标题
            top_level_title_match = re.match(r'#\s*\[([^\]]+)\]', section)
            top_level_title = top_level_title_match.group(1) if top_level_title_match else "General"
            
            # 3. 第二级分割：在每个主要部分内部进行结构化分割
            sub_sections = self._split_by_structural_separators(section, top_level_title)
            all_sub_sections.extend(sub_sections)
            
        # 4. 进一步处理和细分所有子章节
        final_chunks = []
        for section in all_sub_sections:
            section_content = section['content']
            
            # 继承父级元数据并添加当前章节信息
            section_metadata = {
                **(metadata or {}),
                'content_type': self._determine_content_type(section_content),
                'title': section['title'],
                'parent_titles': section['parents']
            }
            
            # 检查章节大小
            section_tokens = self._count_tokens(section_content)
            
            if section_tokens <= self.target_chunk_size * 1.2: # 允许20%的超额
                final_chunks.append({
                    "text": section_content,
                    "metadata": section_metadata
                })
            else:
                # 如果章节太大，则进行更细粒度的分割
                sub_chunks = self._split_large_section(section_content, section_metadata)
                final_chunks.extend(sub_chunks)
        
        # 5. 添加重叠并格式化输出
        return self._format_chunks_with_overlap(final_chunks)

    def _preprocess_text(self, text: str) -> str:
        """预处理文本，清理多余的空行等"""
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _split_by_structural_separators(self, text: str, top_level_parent: str) -> List[Dict]:
        """
        使用正则表达式按Markdown标题和'---'分隔符在单个主区域内分割文本。
        """
        # 分隔符：'---' 或 '#', '##', '###' 标题
        separator_pattern = re.compile(r'(^\s*---\s*$|^#{1,3}\s+.*$)', re.MULTILINE)
        
        separators = list(separator_pattern.finditer(text))
        
        if not separators:
            # 如果没有分隔符，整个区域作为一个块
            title_match = re.match(r'#\s*\[([^\]]+)\]', text)
            title = title_match.group(1) if title_match else top_level_parent
            return [{'level': 1, 'title': title, 'content': text, 'parents': [top_level_parent]}]
            
        sections = []
        # 父标题栈：(level, title)
        parent_titles: List[Tuple[int, str]] = [(0, top_level_parent)]

        # 处理第一个分隔符之前的内容
        if separators and separators[0].start() > 0:
            intro_content = text[:separators[0].start()].strip()
            if intro_content:
                # 尝试从引言中提取标题
                intro_title_match = re.match(r'#\s*\[([^\]]+)\]', intro_content)
                intro_title = intro_title_match.group(1) if intro_title_match else "Introduction"
                sections.append({
                    'level': 1,
                    'title': intro_title,
                    'content': intro_content,
                    'parents': [p for p in parent_titles]
                })
        
        # 遍历所有分隔符，创建章节
        for i, separator in enumerate(separators):
            separator_text = separator.group(1).strip()
            
            level = 0
            title = ""
            
            if separator_text.startswith('---'):
                # --- 作为与 ## 同级的分割
                level = 2 
                # 尝试从内容中提取更有意义的标题
                start_pos_content = separator.end()
                end_pos_content = separators[i+1].start() if i + 1 < len(separators) else len(text)
                content_preview = text[start_pos_content:end_pos_content].strip()
                first_line = content_preview.split('\n')
                
                # 特殊处理人物简介的标题提取
                if top_level_parent == '类脑社区历史人物':
                     title = self._extract_person_name(content_preview)
                elif '###' in first_line: # 如果紧接着是三级标题，用它
                    title = first_line.lstrip('#').strip()
                else:
                    title = "Section"

            elif separator_text.startswith('#'):
                level = separator_text.count('#')
                title = separator_text.lstrip('#').strip().replace('[','').replace(']','') or f"Header Level {level}"

            # 更新父标题栈 - 移除所有级别大于等于当前级别的标题
            while parent_titles and parent_titles[-1][0] >= level:
                parent_titles.pop()
            
            current_parents = [p for p in parent_titles]

            start_pos = separator.end()
            end_pos = separators[i+1].start() if i + 1 < len(separators) else len(text)
            
            content = text[start_pos:end_pos].strip()
            
            if not content:
                continue
            
            # 将分隔符（标题）和内容合并，保留完整上下文
            full_content = f"{separator_text}\n{content}"
            
            sections.append({
                'level': level,
                'title': title,
                'content': full_content,
                'parents': current_parents.copy()
            })

            # 将当前标题压入父标题栈
            parent_titles.append((level, title))
            
        return sections

    def _determine_content_type(self, text: str) -> str:
        """改进的内容类型判断逻辑"""
        lower_text = text.lower()
        
        # 检测Q&A格式 - 更准确的匹配
        if re.search(r'^q\s*[：:]', lower_text, re.MULTILINE) and re.search(r'^a\s*[：:]', lower_text, re.MULTILINE):
            return "qa"
        
        # 检测故障排除格式 - 更宽松的匹配
        troubleshoot_keywords = ['现象', '原因', '解决', '问题', '报错']
        if sum(1 for keyword in troubleshoot_keywords if keyword in text) >= 2:
            return "troubleshooting"
        
        # 检测教程内容
        tutorial_keywords = ['安装', '更新', '步骤', '教程', '指南', '如何']
        if sum(1 for keyword in tutorial_keywords if keyword in lower_text) >= 2:
            return "tutorial"
        
        # 检测人物信息
        if self._is_person_info(text):
            return "person"
            
        return "reference"

    def _is_person_info(self, text: str) -> bool:
        """改进的人物信息检测逻辑"""
        # 检查是否包含人物描述关键词
        person_keywords = [
            '是类脑社区', '管理员', '创作者', '作者', '服主', 'owner',
            '议会', '弹劾', '辞职', '活跃', '贡献', '制作', '建立',
            '2024年', '2025年', '早期', '历史', '重要', '社区'
        ]
        
        keyword_count = sum(1 for keyword in person_keywords if keyword in text.lower())
        
        # 检查人物信息格式模式
        patterns = [
            r'^[^\n]*\([^)]+\)\s*[：:]',  # 姓名(别名):
            r'^[\w\s]+\s*[：:].*(?:管理|创作|作者)',  # 姓名: ...管理/创作/作者
            r'(?:管理员|创作者|服主|Owner).*[\w\s]+',  # 包含职位的描述
        ]
        
        pattern_match = any(re.search(pattern, text, re.MULTILINE | re.IGNORECASE) for pattern in patterns)
        
        return keyword_count >= 2 or pattern_match

    def _extract_person_name(self, text: str) -> str:
        """改进的人物姓名提取"""
        if '## 类脑社区历史人物' in text:
            return '类脑社区历史人物'
            
        # 获取第一行
        first_line = text.split('\n').strip()
        
        # 尝试多种格式匹配
        patterns = [
            r'^([^(：:]+)(?:\([^)]+\))?\s*[:：]',  # 姓名(别名):
            r'^([\w\s]+)(?:\([^)]+\))?\s*[:：]',   # 姓名:
            r'^([^\n]{1,30}?)(?:\s*[:：]|$)',      # 前30字符作为标题
        ]
        
        for pattern in patterns:
            match = re.match(pattern, first_line)
            if match:
                name = match.group(1).strip()
                if name and len(name) > 0:
                    return name
                    
        # 如果都没匹配到，返回截断的第一行
        return first_line[:30].strip() or 'Unknown'

    def _split_large_section(self, text: str, metadata: Dict) -> List[Dict]:
        """
        分割过大的章节
        策略：按段落、句子进行分割
        """
        chunks = []
        # 优先按双换行符（段落）分割
        paragraphs = text.split('\n\n')
        
        current_chunk = ""
        for p in paragraphs:
            p_tokens = self._count_tokens(p)
            current_chunk_tokens = self._count_tokens(current_chunk)
            
            if current_chunk_tokens + p_tokens <= self.target_chunk_size:
                current_chunk += "\n\n" + p if current_chunk else p
            else:
                if current_chunk:
                    chunks.append({"text": current_chunk, "metadata": metadata})
                # 如果单个段落就超长，需要进一步按句子分割 (此处简化，直接作为一个块)
                current_chunk = p
        
        if current_chunk:
            chunks.append({"text": current_chunk, "metadata": metadata})
            
        return chunks

    def _format_chunks_with_overlap(self, chunks: List[Dict]) -> List[Dict]:
        """
        为分块结果添加重叠，并生成最终格式
        """
        final_formatted_chunks = []
        
        for i, chunk in enumerate(chunks):
            # 在当前块的开头添加前一个块的结尾部分作为重叠
            if i > 0 and self.chunk_overlap > 0:
                prev_chunk_text = chunks[i-1]['text']
                # 确保重叠内容来自相同的父级章节，避免不相关的重叠
                if chunks[i-1]['metadata']['parent_titles'] == chunk['metadata']['parent_titles']:
                    overlap_text = self._get_overlap_text(prev_chunk_text)
                    chunk['text'] = overlap_text + "\n...\n" + chunk['text']

            # 更新token计数
            chunk['metadata']['tokens'] = self._count_tokens(chunk['text'])
            final_formatted_chunks.append(chunk)
            
        return final_formatted_chunks

    def _get_overlap_text(self, text: str) -> str:
        """获取用于重叠的文本片段"""
        tokens = self.encoding.encode(text, disallowed_special=())
        if len(tokens) <= self.chunk_overlap:
            return text
        
        overlap_tokens = tokens[-self.chunk_overlap:]
        return self.encoding.decode(overlap_tokens)


# --- 测试代码 ---
async def test_rag_indexer():
    import os
    print("🧪 开始测试 RAGIndexer (优化版)...")
    
    # 读取知识库文件
    knowledge_file = "rag_prompt/ALL.txt"
    if not os.path.exists(knowledge_file):
        print(f"❌ 知识库文件不存在: {knowledge_file}")
        return
        
    with open(knowledge_file, 'r', encoding='utf-8') as f:
        content = f.read()
        
    # 创建索引器实例
    indexer = RAGIndexer(chunk_size=400, chunk_overlap=50)
    
    # 执行智能分块
    chunks = indexer.smart_split(content, metadata={'source': knowledge_file})
    
    print(f"\n✅ 智能分块完成，共生成 {len(chunks)} 个块。")
    
    # 打印一些示例块
    print("\n--- 示例块 ---")
    for i, chunk in enumerate(chunks):
        # 仅打印一些有代表性的块
        if i < 3 or i > len(chunks) - 4:
            print(f"\n块 {i+1}:")
            print(f"  元数据: {chunk['metadata']}")
            print(f"  内容预览: {chunk['text'][:200].strip().replace(chr(10), ' ')}...")
        elif i == 3:
            print("\n...")

class RAGIndexerCog(commands.Cog):
    """RAG索引器Cog，提供文档智能分块功能"""
    
    def __init__(self, bot):
        self.bot = bot
        self.indexer = RAGIndexer()
    
    @commands.command(name="test_indexer")
    async def test_indexer_command(self, ctx):
        """测试RAG索引器功能"""
        await ctx.send("RAG索引器功能正常运行")

async def setup(bot):
    """设置函数，用于加载cog"""
    await bot.add_cog(RAGIndexerCog(bot))

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_rag_indexer())