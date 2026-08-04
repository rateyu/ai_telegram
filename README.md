# Telegram + LiteLLM 机器人

一个通过已安装的 LiteLLM / OpenAI 兼容接口连接模型的 Telegram 机器人，并可通过 LLM tool-calling 或确定性命令控制家庭设备的开机/休眠。

## 更新日志

- 2026-08-04：新增家庭设备电源控制（wake/sleep/status），基于 `~/github/homemachines/home_machines.py`（即 `hm` 命令）。提供两条入口：
  - LLM tool-calling：自然语言（"把电脑都叫醒" "该睡了"）由模型判断是否调用 `home_machine_control` 工具。
  - 确定性命令：`/wake [机器名|all]`、`/sleep [机器名|all]`，不经过模型，直接执行，作为可靠兜底。
  - 两条路径共用同一个 `tools.py::dispatch_tool_call()`，统一做管理员白名单鉴权、目标校验、`sleep` 二次确认（Telegram inline button）、审计日志。
  - 自保护：LiteLLM/llama.cpp 后端所在的机器（当前是 `win-8`，从 `LITELLM_BASE_URL` 自动识别）会被排除在 `sleep all` 之外，且拒绝对它单独下发 `sleep`，避免模型把自己依赖的推理服务器睡了。
  - 详见下方「家庭设备电源控制」一节。

## 运行前提

1. 创建 Telegram bot，并拿到 `TELEGRAM_BOT_TOKEN`。
2. LiteLLM 已配置并可访问。当前默认配置按你的环境设置为：

```yaml
- model_name: qwythos-local
  litellm_params:
    model: openai/qwythos-local
    api_base: http://192.168.0.8:4000/v1
    api_key: sk-none
```

对应机器人 `.env`：

```env
TELEGRAM_PROXY_URL=http://127.0.0.1:1082
LITELLM_BASE_URL=http://192.168.0.8:4000/v1
LITELLM_API_KEY=sk-none
LITELLM_MODEL=qwythos-local
```

可选配置：

```env
REQUEST_TIMEOUT_SECONDS=60
MAX_HISTORY_MESSAGES=20
MAX_REPLY_CHARS=3900
MAX_MODEL_TOKENS=512
REQUEST_RETRIES=2
RETRY_BACKOFF_SECONDS=2
```

如果当前网络可以直连 Telegram，可以留空 `TELEGRAM_PROXY_URL`。使用 Shadowrocket 时，本机代理端口通常类似 `http://127.0.0.1:1082`。

注意：`LITELLM_API_KEY` 是机器人访问 LiteLLM 代理时传给代理的客户端密钥。如果 LiteLLM 代理启用了主密钥、虚拟密钥或数据库鉴权，这里要填代理接受的密钥，不一定等于 `litellm_params.api_key`。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```env
TELEGRAM_BOT_TOKEN=你的 Telegram bot token
TELEGRAM_PROXY_URL=http://127.0.0.1:1082
LITELLM_BASE_URL=http://192.168.0.8:4000/v1
LITELLM_API_KEY=sk-none
LITELLM_MODEL=qwythos-local
```

如果依赖已安装，只需要准备 `.env` 即可。

## 测试 LiteLLM

启动机器人前可以先验证模型接口：

```bash
curl http://192.168.0.8:4000/v1/chat/completions \
  -H 'Authorization: Bearer sk-none' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwythos-local",
    "messages": [{"role": "user", "content": "回复 OK"}],
    "max_tokens": 8
  }'
```

## 启动

日常运行使用重启脚本，脚本会先停止已有实例，再通过 macOS `launchd` 启动唯一一个机器人进程：

```bash
scripts/restart_bot.sh
```

日志写入 `bot.log`，当前进程 ID 写入 `bot.pid`。这两个文件只用于本地运行，已在 `.gitignore` 中忽略。

开发调试时也可以前台运行：

```bash
source .venv/bin/activate
python bot.py
```

如果需要检查后台服务状态：

```bash
launchctl print gui/$(id -u)/com.myu.ai-telegram-bot
ps auxww | grep "/Users/myu/github/ai_telegram/bot.py"
tail -n 120 bot.log
```

## 使用

- 私聊机器人：直接发送消息。
- 群聊：回复机器人的消息，或使用 `/ask 你的问题`。
- `/model` 查看当前 LiteLLM 接口地址和模型名。
- `/health` 检查机器人到 LiteLLM 模型链路是否正常。
- `/reset` 清空当前聊天上下文。
- `/wake [机器名|all]` 唤醒家庭设备（仅管理员，默认 `all`）。
- `/sleep [机器名|all]` 休眠家庭设备（仅管理员，默认 `all`，需点击确认按钮）。
- `/help` 查看命令。
- 也可以直接说自然语言，比如"把电脑都叫醒"，模型会按需调用同一套工具。

上下文按 Telegram chat 隔离，并保存在进程内存中；重启后会清空。工具调用本身（tool_calls/tool 消息）不写入长期上下文，只有最终的自然语言回复会被记住，避免 `MAX_HISTORY_MESSAGES` 截断把一次工具调用切成不完整的消息对而导致下次请求报错。

## 家庭设备电源控制

依赖 `~/github/homemachines/home_machines.py`（本机 `hm` 命令的实际脚本），通过 Wake-on-LAN 唤醒、SSH 执行 suspend/hibernate 休眠。机器人不会调用 `hm` 这个 shell alias（子进程环境里不存在 alias），而是直接执行脚本路径：见 `HM_SCRIPT_PATH` / `HM_PYTHON_BIN`。

### 配置

```env
# 留空则彻底禁用电源控制功能（fail closed）。填你的 Telegram 数字 user id（问 @userinfobot 拿）。
TELEGRAM_ADMIN_USER_IDS=123456789,987654321
HM_SCRIPT_PATH=/Users/myu/github/homemachines/home_machines.py
HM_MACHINES_CONFIG=/Users/myu/github/homemachines/machines.json
HM_PYTHON_BIN=            # 留空则用机器人自己的 venv python（home_machines.py 只依赖标准库，够用）
HM_COMMAND_TIMEOUT_SECONDS=150
```

### 安全设计

- **鉴权在执行层，不在 prompt 里**：无论是模型主动发起的 tool_call，还是 `/wake` `/sleep` 命令，最终都进 `tools.py::dispatch_tool_call()`，在真正执行前检查 `telegram_user_id` 是否在 `TELEGRAM_ADMIN_USER_IDS` 白名单里。非管理员在群聊里让模型"帮我关机"，模型即使决定调用工具，也会被拒绝执行。
- **目标白名单**：`target` 只接受 `machines.json` 里已存在的机器名或 `all`，不接受自由文本，杜绝模型编造机器名或注入参数。
- **自保护**：LiteLLM 后端所在机器（自动从 `LITELLM_BASE_URL` 的 host 匹配 `machines.json` 里的 `ip`）——
  - `sleep all` 时自动跳过它，并在结果里注明。
  - 直接指定 `sleep <该机器>` 会被直接拒绝，因为休眠它可能在请求处理到一半时就切断机器人依赖的推理服务。这类操作需要手动处理。
- **`sleep` 需要二次确认**：无论来自模型还是 `/sleep` 命令，都会先给出一个 inline button（"确认执行" / "取消"），2 分钟内有效，只有发起者本人或管理员可以点击确认；`wake`/`status` 无副作用风险，直接执行。
- **审计日志**：每次工具调用（无论执行、拒绝还是待确认）都记录到 `bot.log`，包含 `telegram_user_id`、`action`、`target`、执行结果。
- **原始输出透传**：`hm` 脚本的原始 stdout 会直接发给 Telegram 用户（而不是只让模型转述），保证"设备到底发生了什么"有可核对的真实来源。

## 常见问题

如果启动时日志出现 `LiteLLM health check failed`，机器人仍会启动，但模型调用不可用。优先检查：

- `http://192.168.0.8:4000/v1` 当前机器是否能访问。
- LiteLLM 中是否存在 `model_name: qwythos-local`。
- `.env` 中的 `LITELLM_MODEL` 是否和 LiteLLM 暴露的 `model_name` 一致。
- 如果返回 `401 Authentication Error`，把 `.env` 的 `LITELLM_API_KEY` 改成 LiteLLM 代理接受的客户端密钥。
- 如果返回 `502`、`503` 或 `504`，通常是 LiteLLM 已响应，但后端 llama.cpp 服务仍在启动、模型还没加载完成，或 LiteLLM 到 llama.cpp 的 `api_base` 不可达。等待模型加载完成后可用 `/health` 重新检查。

机器人会对 `408`、`409`、`429`、`500`、`502`、`503`、`504`、连接中断和超时做短暂重试。可以通过 `REQUEST_RETRIES` 和 `RETRY_BACKOFF_SECONDS` 调整重试次数和退避时间；如果模型经常返回空内容，可以设置 `MAX_MODEL_TOKENS` 给 LiteLLM 请求增加明确的输出 token 上限。

如果 Telegram 日志出现 `Conflict: terminated by other getUpdates request`，说明同一个机器人 token 有多个轮询实例在运行。先执行 `scripts/restart_bot.sh`，它会清理本项目的旧进程并重启 launchd 服务；如果仍然冲突，再检查其他机器或服务器是否也在运行同一个 token。
