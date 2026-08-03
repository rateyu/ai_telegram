# Telegram + LiteLLM Bot

一个通过已安装的 LiteLLM / OpenAI 兼容接口连接模型的 Telegram 机器人。

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

对应 bot `.env`：

```env
LITELLM_BASE_URL=http://192.168.0.8:4000/v1
LITELLM_API_KEY=sk-none
LITELLM_MODEL=qwythos-local
```

注意：`LITELLM_API_KEY` 是 bot 访问 LiteLLM proxy 时传给 proxy 的客户端 token。如果 LiteLLM proxy 启用了 master key、virtual key 或数据库鉴权，这里要填 proxy 接受的 key，不一定等于 `litellm_params.api_key`。

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
LITELLM_BASE_URL=http://192.168.0.8:4000/v1
LITELLM_API_KEY=sk-none
LITELLM_MODEL=qwythos-local
```

如果依赖已安装，只需要准备 `.env` 即可。

## 测试 LiteLLM

启动 bot 前可以先验证模型接口：

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

```bash
source .venv/bin/activate
python bot.py
```

## 使用

- 私聊 bot：直接发送消息。
- 群聊：回复 bot 的消息，或使用 `/ask 你的问题`。
- `/model` 查看当前 LiteLLM endpoint 和模型名。
- `/reset` 清空当前聊天上下文。
- `/help` 查看命令。

上下文按 Telegram chat 隔离，并保存在进程内存中；重启后会清空。

## 常见问题

如果启动时日志出现 `LiteLLM health check failed`，bot 仍会启动，但模型调用不可用。优先检查：

- `http://192.168.0.8:4000/v1` 当前机器是否能访问。
- LiteLLM 中是否存在 `model_name: qwythos-local`。
- `.env` 中的 `LITELLM_MODEL` 是否和 LiteLLM 暴露的 `model_name` 一致。
- 如果返回 `401 Authentication Error`，把 `.env` 的 `LITELLM_API_KEY` 改成 LiteLLM proxy 接受的客户端 key。
