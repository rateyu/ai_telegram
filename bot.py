import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import APIStatusError, AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai._base_client").setLevel(logging.WARNING)
logger = logging.getLogger("telegram-litellm-bot")

TELEGRAM_MESSAGE_LIMIT = 4096


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    litellm_base_url: str
    litellm_api_key: str
    litellm_model: str
    bot_system_prompt: str
    request_timeout_seconds: float
    max_history_messages: int
    max_reply_chars: int


def load_settings() -> Settings:
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_bot_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Copy .env.example to .env and fill it in.")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        litellm_base_url=os.getenv("LITELLM_BASE_URL", "http://192.168.0.8:4000/v1").strip(),
        litellm_api_key=os.getenv("LITELLM_API_KEY", "sk-none").strip(),
        litellm_model=os.getenv("LITELLM_MODEL", "qwythos-local").strip(),
        bot_system_prompt=os.getenv("BOT_SYSTEM_PROMPT", "你是一个简洁、可靠的 Telegram AI 助手。").strip(),
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "60")),
        max_history_messages=int(os.getenv("MAX_HISTORY_MESSAGES", "20")),
        max_reply_chars=min(
            int(os.getenv("MAX_REPLY_CHARS", "3900")),
            TELEGRAM_MESSAGE_LIMIT,
        ),
    )


settings = load_settings()
client = AsyncOpenAI(
    api_key=settings.litellm_api_key,
    base_url=settings.litellm_base_url,
    timeout=settings.request_timeout_seconds,
)

chat_histories: dict[int, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=settings.max_history_messages)
)


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    if not bot_username:
        return text.strip()
    return re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()


def should_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or not message.text:
        return False

    if chat.type == ChatType.PRIVATE:
        return True

    if message.text.startswith("/ask"):
        return True

    bot_username = context.bot.username
    if bot_username and f"@{bot_username.lower()}" in message.text.lower():
        return True

    reply_to = message.reply_to_message
    return bool(reply_to and reply_to.from_user and reply_to.from_user.id == context.bot.id)


def extract_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    text = update.effective_message.text or ""
    bot_username = context.bot.username
    if text.startswith("/ask"):
        parts = text.split(maxsplit=1)
        return strip_bot_mention(parts[1], bot_username) if len(parts) > 1 else ""
    return strip_bot_mention(text, bot_username)


def split_reply(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        chunk = remaining[:max_chars]
        split_at = max(chunk.rfind("\n\n"), chunk.rfind("\n"), chunk.rfind("。"), chunk.rfind(". "))
        if split_at > max_chars * 0.5:
            chunk = remaining[: split_at + 1]
        chunks.append(chunk.strip())
        remaining = remaining[len(chunk) :].strip()
    return chunks


async def reply_long_text(update: Update, text: str) -> None:
    for chunk in split_reply(text, settings.max_reply_chars):
        await update.effective_message.reply_text(chunk)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "已连接。私聊我可以直接提问；群聊中请使用 /ask，或回复/提及我。"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "/ask 问题 - 在群聊中向我提问\n"
        "/model - 查看当前 LiteLLM 连接配置\n"
        "/reset - 清空当前聊天上下文\n"
        "/help - 查看帮助"
    )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "当前模型配置：\n"
        f"model: {settings.litellm_model}\n"
        f"base_url: {settings.litellm_base_url}"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    chat_histories.pop(chat_id, None)
    await update.effective_message.reply_text("已清空当前聊天上下文。")


async def ask_litellm(chat_id: int, prompt: str) -> str:
    history = chat_histories[chat_id]
    messages = [{"role": "system", "content": settings.bot_system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    response = await client.chat.completions.create(
        model=settings.litellm_model,
        messages=messages,
    )
    answer = response.choices[0].message.content or ""

    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": answer})
    return answer.strip() or "模型没有返回内容。"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not should_answer(update, context):
        return

    prompt = extract_prompt(update, context)
    if not prompt:
        await update.effective_message.reply_text("请把问题发给我。")
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        answer = await ask_litellm(chat_id, prompt)
    except asyncio.TimeoutError:
        logger.exception("LiteLLM request timed out")
        await update.effective_message.reply_text("请求模型超时，请稍后再试。")
        return
    except APIStatusError as exc:
        logger.exception("LiteLLM returned an API error")
        if exc.status_code == 401:
            await update.effective_message.reply_text(
                "LiteLLM 鉴权失败：请把 .env 里的 LITELLM_API_KEY 改成 LiteLLM proxy 接受的客户端 key。"
            )
            return
        await update.effective_message.reply_text(
            "LiteLLM 返回错误，请检查模型服务和配置。\n"
            f"endpoint: {settings.litellm_base_url}\n"
            f"model: {settings.litellm_model}\n"
            f"status: {exc.status_code}"
        )
        return
    except Exception as exc:
        logger.exception("LiteLLM request failed")
        await update.effective_message.reply_text(
            "调用 LiteLLM 失败，请检查模型服务和配置。\n"
            f"endpoint: {settings.litellm_base_url}\n"
            f"model: {settings.litellm_model}\n"
            f"error: {exc}"
        )
        return

    await reply_long_text(update, answer)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram update failed", exc_info=context.error)


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    logger.info("Telegram bot authorized as @%s", me.username)

    try:
        response = await client.chat.completions.create(
            model=settings.litellm_model,
            messages=[
                {"role": "system", "content": "只返回 OK。"},
                {"role": "user", "content": "健康检查"},
            ],
        )
        answer = response.choices[0].message.content or ""
        if not answer.strip():
            raise RuntimeError("LiteLLM health check returned empty content")
        logger.info("LiteLLM health check passed")
    except Exception:
        logger.exception("LiteLLM health check failed; bot will still start")


def main() -> None:
    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("ask", handle_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info("Bot started. LiteLLM endpoint: %s model: %s", settings.litellm_base_url, settings.litellm_model)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
