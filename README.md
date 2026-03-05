# Discord 答疑机器人

这是一个面向 Discord 服务器的多功能 AI 机器人，核心链路为：
- `cogs/appdayi.py`：快速答疑（全量提示词直发模型）
- `cogs/mention.py`：被 @ 后自动回复（全量提示词直发模型）

## ✨ 核心特性

- **快速答疑（支持图片）**：右键消息即可触发答疑，自动处理文本与图片输入。
- **自动回复与线程管理**：支持按配置在指定线程/频道响应提及。
- **总结功能**：支持多模板消息总结（判决/辩论/聊天/复盘/提问/自动）。
- **模块化设计**：基于 `cogs` 拆分功能，便于维护和扩展。

---

## 🔧 安装与配置

1. **克隆仓库**
   ```bash
   git clone https://github.com/your-repo/your-bot.git
   cd your-bot
   ```

2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

3. **创建配置文件**
   - 将 `.env.example` 复制为 `.env`
   - 填写 `DISCORD_BOT_TOKEN`、`OPENAI_API_KEY`、`OPENAI_API_BASE_URL`、`OPENAI_MODEL` 等必要项

4. **准备提示词文件**
   - `prompt/ALL.txt`：`appdayi` 与 `mention` 等答疑链路使用的主提示词
   - `summary_prompt/*.txt`：`summary` 功能使用的模板提示词

5. **运行机器人**
   ```bash
   python bot.py
   ```

---

## 📁 项目结构概览

```text
.
├── cogs/                   # 主要功能模块
│   ├── appdayi.py          # 快速答疑
│   ├── mention.py          # 提及自动回复
│   ├── summary.py          # 消息总结
│   └── ...
├── prompt/                 # 答疑主提示词目录（ALL.txt 等）
├── summary_prompt/         # summary 专用提示词目录
├── app_temp/               # 运行时临时文件
├── app_save/               # 提示词归档
├── bot.py                  # 机器人主入口
└── requirements.txt        # Python 依赖
```
