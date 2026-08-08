# Telegram + LiteLLM 机器人

一个通过已安装的 LiteLLM / OpenAI 兼容接口连接模型的 Telegram 机器人，并可通过 LLM tool-calling 或确定性命令控制家庭设备的开机/休眠。

## 更新日志

按提交时间倒序，每条对应一个 git commit（`git log --oneline` 可查完整历史）。详细用法见下方「家庭设备电源控制」一节，这里只记录变了什么、为什么变。

- **`be8684f`** 2026-08-04 — `win-8` 支持休眠 + LiteLLM 不可达时的关键词兜底
  - 不再硬拒绝对 `win-8`（LLM 推理服务所在机器）下发 `sleep`。原先的自保护假设"确认后还要模型再总结一次"，但实际上 `sleep` 的确认点击本来就不经过模型（回调直接执行），所以休眠它本身是安全的，现在只保留一句提示。
  - `sleep all` 不再手动排除 `win-8`、逐台循环调用，改成把 `target="all"` 原样交给 `hm` 处理——顺带修掉一个隐患：逐台调用会跳过 `home_machines.py` 里"先睡 jump host 后面的机器、再睡 jump host 本身"的顺序保护，现在这个顺序由 `hm` 自己统一保证。
  - 新增 `is_host_reachable()`（2 秒 TCP 探活）+ `parse_intent()`（保守的中英文关键词解析）：模型所在机器睡着、LiteLLM 连不上时，自然语言消息自动降级为关键词直接执行，不再依赖一个连不上的模型才能把它叫醒。
- **`597a33c`** 2026-08-04 — 补充电源控制功能的使用步骤到文档
- **`686ca3a`** 2026-08-04 — 新增家庭设备电源控制（wake/sleep/status）
  - 基于 `~/github/homemachines/home_machines.py`（`hm` 命令）新增两条触发入口：LLM tool-calling（自然语言）+ `/wake` `/sleep` 确定性命令，共用 `tools.py::dispatch_tool_call()` 做鉴权、校验、二次确认、审计日志。
- **`20d501f`** 及更早 — 见 `git log`，主要是首次提交和稳定性/重启流程加固，与电源控制功能无关。

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
MODEL_LIST_CACHE_SECONDS=300
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
- `/model` 查看/切换当前对话使用的模型（按钮选择，见下方「多模型选择」一节）；也支持 `/model <名称>` 和 `/model refresh`。
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

### 使用步骤

1. 在 `.env` 填 `TELEGRAM_ADMIN_USER_IDS`（问 @userinfobot 拿自己的 Telegram 数字 user id），留空则功能整体禁用；改完 `.env` 后用 `scripts/restart_bot.sh` 重启生效。
2. 自然语言方式：私聊或群聊里直接说"帮我把电脑都叫醒"、"win-66 在线吗"、"该睡了，都休眠吧"，模型会判断是否调用 `home_machine_control` 工具，非管理员发起会被直接拒绝。
3. 确定性命令方式：`/wake all`（或 `/wake win-8` 指定单台）唤醒，立即执行；`/sleep all`（或 `/sleep linux-1153`）休眠。
4. `sleep` 无论来自自然语言、命令还是关键词兜底，都会先收到一条带「✅ 确认执行 / ❌ 取消」按钮的消息，2 分钟内点击确认才真正执行，只有发起人本人或管理员能点；不确认会自动过期。
5. 执行后机器人会把 `hm` 脚本的原始输出直接发回聊天，可直接核对真实结果，不必只信模型转述。

**测试 `win-8` 休眠 + 关键词兜底唤醒的完整链路：**

```text
你：/sleep win-8                      → 弹出确认按钮，带"模型暂时不可用"提示
你：点击「✅ 确认执行」                 → win-8 开始休眠，LiteLLM 随之不可达
你：（等几秒后）唤醒 win-8              → 这条消息发出前机器人会先探活 LiteLLM 失败，
                                         自动降级为关键词解析，不经过模型直接发送 WOL
你：（win-8 上线、LiteLLM 起来后）问点别的 → 探活恢复成功，自动切回正常的模型对话
```

### 安全设计

- **鉴权在执行层，不在 prompt 里**：无论是模型主动发起的 tool_call，还是 `/wake` `/sleep` 命令，最终都进 `tools.py::dispatch_tool_call()`，在真正执行前检查 `telegram_user_id` 是否在 `TELEGRAM_ADMIN_USER_IDS` 白名单里。非管理员在群聊里让模型"帮我关机"，模型即使决定调用工具，也会被拒绝执行。
- **目标白名单**：`target` 只接受 `machines.json` 里已存在的机器名或 `all`，不接受自由文本，杜绝模型编造机器名或注入参数。
- **`sleep` 需要二次确认**：无论来自模型、`/sleep` 命令还是关键词兜底，都会先给出一个 inline button（"确认执行" / "取消"），2 分钟内有效，只有发起者本人或管理员可以点击确认；`wake`/`status` 无副作用风险，直接执行。确认点击本身不再经过模型（`handle_confirmation_callback` 直接执行），这也是为什么休眠 LLM 所在机器是安全的——不存在"执行到一半模型被掐断"的时序问题。
- **LLM 所在机器（`win-8`）的提示而非拦截**：休眠它或 `sleep all` 时，确认提示和执行结果里都会带一句"休眠后模型对话暂时不可用，需重新唤醒"的说明，但不会拦截操作。
- **模型不可达时的关键词兜底**：每条消息处理前先探活 LiteLLM（`is_host_reachable()`，2 秒 TCP 超时）；探活失败就跳过模型，改用 `parse_intent()` 做保守的关键词匹配（唤醒/休眠/状态 + 机器名/全部），直接执行同一个 `dispatch_tool_call()`。识别不到就提示用户改用 `/wake` `/sleep` 命令，不会瞎猜目标。这条路径专门解决"模型所在机器睡着了，没法用自然语言把它叫醒"的鸡生蛋问题。
- **审计日志**：每次工具调用（无论执行、拒绝还是待确认）都记录到 `bot.log`，包含 `telegram_user_id`、`action`、`target`、执行结果。
- **原始输出透传**：`hm` 脚本的原始 stdout 会直接发给 Telegram 用户（而不是只让模型转述），保证"设备到底发生了什么"有可核对的真实来源。

## 多模型选择

机器人不在代码里写死可选模型列表，而是每次调用 LiteLLM 的 `GET /v1/models`（OpenAI 兼容接口）实时拿它当前暴露的模型集合，带一个短 TTL 缓存。换句话说：**在 LiteLLM 侧加一个模型（`config.yaml` 新增一条 `model_name` + reload），Telegram 这边立刻就能选，不需要改 `ai_telegram` 代码、不需要重新部署机器人。**

### 使用

- `/model` — 弹出按钮列表（当前选中的带 ✅），点按钮即可切换。
- `/model <名称>` — 直接切换到指定模型（名称需在 `/v1/models` 返回的列表里）。
- `/model refresh` — 强制刷新一次列表缓存（刚在 LiteLLM 加了新模型、还没到缓存过期时间时用）。

模型选择按 Telegram chat 独立记忆（存在进程内存里，重启后回到 `LITELLM_MODEL` 默认值），不同群/不同私聊可以各选各的模型，互不影响；对话上下文（`chat_histories`）不随模型切换清空，切换后历史消息照常带入下一次请求。

### 配置

```env
# 缓存 LiteLLM /v1/models 列表的秒数，过期后 /model 会自动重新拉取一次。
MODEL_LIST_CACHE_SECONDS=300
```

### 和电源控制、win-8 兜底的关系

`home_machine_control` 这个 tool 的 schema 对所有模型一视同仁——只要选中的模型支持 OpenAI 风格的 function calling，"唤醒 win-8"这类自然语言在任何模型下都能触发同一个 `dispatch_tool_call()`。

目前 LiteLLM 本身仍然跑在 `win-8` 上，所以**不管当前选的是哪个模型**，只要 `win-8` 关机，所有模型都会一起不可达——`is_host_reachable()` 探活的是 `LITELLM_BASE_URL` 这一个地址，不区分模型。这种情况下仍然走关键词兜底（见「家庭设备电源控制」一节）来唤醒 `win-8`，等它和 LiteLLM 恢复后再自动切回正常对话。等 LiteLLM 迁移到常驻服务器后，这层探活/兜底就不再必要，但目前先保留。

## 常见问题

如果启动时日志出现 `LiteLLM health check failed`，机器人仍会启动，但模型调用不可用。优先检查：

- `http://192.168.0.8:4000/v1` 当前机器是否能访问。
- LiteLLM 中是否存在 `model_name: qwythos-local`。
- `.env` 中的 `LITELLM_MODEL` 是否和 LiteLLM 暴露的 `model_name` 一致。
- 如果返回 `401 Authentication Error`，把 `.env` 的 `LITELLM_API_KEY` 改成 LiteLLM 代理接受的客户端密钥。
- 如果返回 `502`、`503` 或 `504`，通常是 LiteLLM 已响应，但后端 llama.cpp 服务仍在启动、模型还没加载完成，或 LiteLLM 到 llama.cpp 的 `api_base` 不可达。等待模型加载完成后可用 `/health` 重新检查。

机器人会对 `408`、`409`、`429`、`500`、`502`、`503`、`504`、连接中断和超时做短暂重试。可以通过 `REQUEST_RETRIES` 和 `RETRY_BACKOFF_SECONDS` 调整重试次数和退避时间；如果模型经常返回空内容，可以设置 `MAX_MODEL_TOKENS` 给 LiteLLM 请求增加明确的输出 token 上限。

如果 Telegram 日志出现 `Conflict: terminated by other getUpdates request`，说明同一个机器人 token 有多个轮询实例在运行。先执行 `scripts/restart_bot.sh`，它会清理本项目的旧进程并重启 launchd 服务；如果仍然冲突，再检查其他机器或服务器是否也在运行同一个 token。
