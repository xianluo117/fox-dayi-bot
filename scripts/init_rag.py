"""
RAG系统初始化脚本
用于初始化向量数据库并索引知识库
"""

import sys
import os
import asyncio
from pathlib import Path

# 添加项目根目录到系统路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from cogs.rag_processor import RAGProcessor

# 加载环境变量
load_dotenv()


async def init_rag_system():
    """初始化RAG系统"""
    print("🚀 开始初始化RAG系统...")
    print("=" * 50)
    # 显示当前选择的向量化API来源与模型（与 RAGProcessor 保持一致的默认逻辑）
    embedding_backend = os.getenv("EMBEDDING_BACKEND", "siliconflow").lower()
    embedding_model = os.getenv("EMBEDDING_MODEL") or (
        "BAAI/bge-large-zh-v1.5" if embedding_backend == "siliconflow" else "text-embedding-3-small"
    )
    print(f"🔎 当前文本向量化后端: {embedding_backend}")
    print(f"🔎 当前文本模型: {embedding_model}")
    
    # 根据所选后端检查必要的环境变量
    if embedding_backend == "siliconflow":
        if not os.getenv("SILICONFLOW_API_KEY"):
            print("❌ 错误：当前后端为 siliconflow，但未配置 SILICONFLOW_API_KEY")
            return False
    else:
        if not (os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")):
            print("❌ 错误：当前后端为 openai，但未配置 OPENAI_API_KEY 或 EMBEDDING_API_KEY")
            return False
         
    # 检查是否启用RAG
    rag_enabled = os.getenv("RAG_ENABLED", "false").lower() == "true"
    if not rag_enabled:
        print("⚠️ 警告：RAG系统未启用（RAG_ENABLED=false）")
        print("提示：请在.env文件中设置 RAG_ENABLED=true 来启用RAG系统")
        
    # 创建RAG处理器
    try:
        processor = RAGProcessor()
        print("✅ RAG处理器创建成功")
    except Exception as e:
        print(f"❌ 创建RAG处理器失败: {e}")
        return False
        
    # 获取系统状态
    stats = processor.get_stats()
    print("\n📊 当前系统状态:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    
    # 询问是否要索引知识库
    print("\n" + "=" * 50)
    response = input("是否要索引知识库文件？(y/n): ").strip().lower()
    
    if response == 'y':
        # 询问知识库文件路径
        knowledge_file = input("\n请输入知识库文件路径（默认: prompt/ALL.txt）: ").strip()
        if not knowledge_file:
            knowledge_file = "prompt/ALL.txt"
        
        # 检查文件是否存在
        if not os.path.exists(knowledge_file):
            print(f"❌ 知识库文件不存在: {knowledge_file}")
            
            # 列出可用的知识库文件
            prompt_dir = "prompt"
            if os.path.exists(prompt_dir):
                files = [f for f in os.listdir(prompt_dir) if f.endswith('.txt')]
                if files:
                    print(f"\n可用的知识库文件:")
                    for i, f in enumerate(files, 1):
                        print(f"  {i}. {f}")
                    
                    choice = input("\n请选择文件编号（或输入文件路径）: ").strip()
                    
                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(files):
                            knowledge_file = os.path.join(prompt_dir, files[idx])
                    except ValueError:
                        knowledge_file = choice
            
            if not os.path.exists(knowledge_file):
                print(f"❌ 文件不存在: {knowledge_file}")
                return False
        
        # 询问是否清空现有数据
        if stats.get("total_chunks", 0) > 0:
            clear = input(f"\n⚠️ 数据库中已有 {stats['total_chunks']} 个文档块，是否清空？(y/n): ").strip().lower()
            if clear == 'y':
                processor.clear_database()
                print("✅ 数据库已清空")
        
        # 读取并索引文件
        print(f"\n📖 正在读取文件: {knowledge_file}")
        try:
            with open(knowledge_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            print(f"📄 文件大小: {len(content)} 字符")
            
            # 索引文档
            print("\n🔄 开始索引文档...")
            chunk_count = await processor.index_document(content, source=knowledge_file)
            
            print(f"\n✅ 索引完成！")
            print(f"  - 生成了 {chunk_count} 个文档块")
            
            # 测试检索
            print("\n" + "=" * 50)
            test_query = input("输入测试查询（直接回车跳过）: ").strip()
            
            if test_query:
                print(f"\n🔍 测试查询: {test_query}")
                contexts = await processor.retrieve_context(test_query, top_k=3)
                
                if contexts:
                    print(f"\n找到 {len(contexts)} 个相关文档:")
                    for i, ctx in enumerate(contexts, 1):
                        print(f"\n--- 文档 {i} (相似度: {ctx['similarity']:.2f}) ---")
                        # 显示前200个字符
                        preview = ctx['text'][:200] + "..." if len(ctx['text']) > 200 else ctx['text']
                        print(preview)
                else:
                    print("❌ 未找到相关文档")
                    
        except Exception as e:
            print(f"❌ 处理文件时出错: {e}")
            return False
    
    # 最终统计
    print("\n" + "=" * 50)
    final_stats = processor.get_stats()
    print("📊 最终系统状态:")
    for key, value in final_stats.items():
        print(f"  {key}: {value}")
    
    print("\n✅ RAG系统初始化完成！")
    
    if not rag_enabled:
        print("\n⚠️ 提醒：RAG系统当前未启用")
        print("要启用RAG功能，请在.env文件中设置:")
        print("  RAG_ENABLED=true")
    
    return True


async def test_simple_chunking():
    """测试简单的分块功能"""
    from cogs.rag_processor import simple_chunk_text
    
    print("\n🧪 测试简单分块功能...")
    print("=" * 50)
    
    test_text = """
    SillyTavern是一个用户友好的界面，用于与AI语言模型进行交互。
    它支持多种API，包括OpenAI、Claude、Gemini等。
    
    安装步骤：
    1. 下载最新版本
    2. 解压文件
    3. 运行启动脚本
    
    如果遇到问题，请查看常见问题解答部分。
    """
    
    chunks = simple_chunk_text(test_text, max_tokens=30, overlap=5)
    
    print(f"原文本长度: {len(test_text)} 字符")
    print(f"生成了 {len(chunks)} 个文本块\n")
    
    for i, chunk in enumerate(chunks, 1):
        print(f"块 {i}:")
        print(f"  {chunk}")
        print()


def main():
    """主函数"""
    print("=" * 50)
    print("RAG系统初始化工具")
    print("=" * 50)
    
    while True:
        print("\n请选择操作:")
        print("1. 初始化RAG系统")
        print("2. 测试简单分块功能")
        print("3. 退出")
        
        choice = input("\n请输入选项 (1-3): ").strip()
        
        if choice == '1':
            asyncio.run(init_rag_system())
        elif choice == '2':
            asyncio.run(test_simple_chunking())
        elif choice == '3':
            print("👋 再见！")
            break
        else:
            print("❌ 无效选项，请重新选择")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 程序被中断")
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        import traceback
        traceback.print_exc()