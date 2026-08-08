import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import tools as hm_tools

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai._base_client").setLevel(logging.WARNING)
logger = logging.getLogger("telegram-litellm-bot")

TELEGRAM_MESSAGE_LIMIT = 4096
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_proxy_url: str | None
    litellm_base_url: str
    litellm_api_key: str
    litellm_model: str
    bot_system_prompt: str
    request_timeout_seconds: float
    max_history_messages: int
    max_reply_chars: int
    max_model_tokens: int | None
    request_retries: int
    retry_backoff_seconds: float
    admin_user_ids: frozenset[int]
    hm_script_path: str
    hm_python_bin: str
    hm_machines_config: str
    hm_command_timeout_seconds: float
    model_list_cache_seconds: float


def parse_admin_ids(value: str | None) -> frozenset[int]:
    if not value or not value.strip():
        return frozenset()
    ids: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logging.getLogger("telegram-litellm-bot").warning("Ignoring invalid admin id: %r", part)
    return frozenset(ids)


def load_settings() -> Settings:
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_bot_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Copy .env.example to .env and fill it in.")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
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
        max_model_tokens=parse_optional_int(os.getenv("MAX_MODEL_TOKENS")),
        request_retries=max(0, int(os.getenv("REQUEST_RETRIES", "2"))),
        retry_backoff_seconds=max(0.1, float(os.getenv("RETRY_BACKOFF_SECONDS", "2"))),
        admin_user_ids=parse_admin_ids(os.getenv("TELEGRAM_ADMIN_USER_IDS")),
        hm_script_path=os.getenv(
            "HM_SCRIPT_PATH", "/Users/myu/github/homemachines/home_machines.py"
        ).strip(),
        hm_python_bin=os.getenv("HM_PYTHON_BIN", "").strip() or sys.executable,
        hm_machines_config=os.getenv(
            "HM_MACHINES_CONFIG", "/Users/myu/github/homemachines/machines.json"
        ).strip(),
        hm_command_timeout_seconds=float(os.getenv("HM_COMMAND_TIMEOUT_SECONDS", "150")),
        model_list_cache_seconds=float(os.getenv("MODEL_LIST_CACHE_SECONDS", "300")),
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

tools_settings = hm_tools.ToolsSettings(
    hm_script_path=settings.hm_script_path,
    hm_python_bin=settings.hm_python_bin,
    hm_command_timeout_seconds=settings.hm_command_timeout_seconds,
    machines_config_path=settings.hm_machines_config,
    admin_user_ids=settings.admin_user_ids,
)
machine_names = hm_tools.load_machine_names(settings.hm_machines_config)
self_host_machine = hm_tools.resolve_self_host_machine(
    settings.hm_machines_config, settings.litellm_base_url
)
TOOL_SCHEMA = hm_tools.build_tool_schema(machine_names)
TOOL_USAGE_HINT = (
    "如果用户明确要求开机/唤醒、关机/休眠，或查询某台或全部家庭电脑的在线状态，"
    "调用 home_machine_control 工具处理；不确定意图或只是闲聊时不要调用。"
)
TOOL_MAX_ROUNDS = 3
PENDING_CONFIRMATION_TTL_SECONDS = 120

if not settings.admin_user_ids:
    logging.getLogger("telegram-litellm-bot").warning(
        "TELEGRAM_ADMIN_USER_IDS is empty; home machine power control is disabled for everyone."
    )
if not machine_names:
    logging.getLogger("telegram-litellm-bot").warning(
        "No machines loaded from %s; home machine power control tool has no valid targets.",
        settings.hm_machines_config,
    )

# Per-chat model selection. Models themselves are not hardcoded here — they're whatever LiteLLM
# currently exposes on /v1/models, so adding a model on the LiteLLM side (a config.yaml entry +
# reload) shows up here with no bot deploy required. Selection lives in memory only, same as
# chat_histories / pending_confirmations, and resets to the default on bot restart.
chat_model_selection: dict[int, str] = {}
_model_list_cache: list[str] = []
_model_list_cache_at: float = 0.0


async def list_available_models(force: bool = False) -> list[str]:
    """Fetch the model names LiteLLM currently serves, with a short TTL cache.

    Falls back to the last known-good list (or the configured default) if LiteLLM/its backend
    is unreachable, so /model still shows something useful during a win-8-down window instead
    of erroring out.
    """
    global _model_list_cache, _model_list_cache_at
    if not force and _model_list_cache and time.monotonic() - _model_list_cache_at < settings.model_list_cache_seconds:
        return _model_list_cache
    try:
        response = await client.models.list()
        names = sorted(m.id for m in response.data)
        if names:
            _model_list_cache, _model_list_cache_at = names, time.monotonic()
    except Exception:
        logger.warning("list_available_models: fetch failed, using cached/default list", exc_info=True)
    return _model_list_cache or [settings.litellm_model]


def get_active_model(chat_id: int) -> str:
    return chat_model_selection.get(chat_id, settings.litellm_model)


@dataclass
class PendingAction:
    action: str
    target: str
    requester_user_id: int
    chat_id: int
    created_at: float


pending_confirmations: dict[str, PendingAction] = {}


def register_pending_confirmation(chat_id: int, requester_user_id: int, action: str, target: str) -> str:
    token = uuid.uuid4().hex[:12]
    pending_confirmations[token] = PendingAction(
        action=action,
        target=target,
        requester_user_id=requester_user_id,
        chat_id=chat_id,
        created_at=time.monotonic(),
    )
    return token


def pop_valid_pending(token: str) -> PendingAction | None:
    pending = pending_confirmations.pop(token, None)
    if pending is None:
        return None
    if time.monotonic() - pending.created_at > PENDING_CONFIRMATION_TTL_SECONDS:
        return None
    return pending


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
        "/model - 查看/切换当前对话使用的模型（按钮选择）\n"
        "/model <名称> - 直接切换到指定模型\n"
        "/model refresh - 强制刷新模型列表\n"
        "/health - 检查 LiteLLM 模型链路\n"
        "/reset - 清空当前聊天上下文\n"
        "/wake [机器名|all] - 唤醒家庭设备（仅管理员，默认 all）\n"
        "/sleep [机器名|all] - 休眠家庭设备（仅管理员，默认 all，需二次确认）\n"
        "/help - 查看帮助\n\n"
        "也可以直接用自然语言让我开机/关机（比如“把电脑都叫醒”），我会按需调用同一套工具。"
    )


async def run_admin_machine_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not hm_tools.is_authorized(tools_settings, user_id):
        await message.reply_text("你没有控制家庭设备电源的权限。")
        return

    parts = (message.text or "").split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else "all"

    if action == "sleep":
        token = register_pending_confirmation(chat_id, user_id, action, target)
        await send_confirmation_prompt(context, chat_id, token, action, target)
        return

    outcome = await hm_tools.dispatch_tool_call(
        tools_settings, action, target, user_id, machine_names, self_host_machine
    )
    await reply_long_text(update, outcome.user_message or outcome.summary)


async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_admin_machine_command(update, context, action="wake")


async def sleep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_admin_machine_command(update, context, action="sleep")


async def handle_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if ":" not in data:
        return
    kind, token = data.split(":", 1)

    pending = pop_valid_pending(token)
    if pending is None:
        await query.edit_message_text("该确认已过期或已被处理。")
        return

    requester_id = query.from_user.id
    if requester_id != pending.requester_user_id and requester_id not in tools_settings.admin_user_ids:
        pending_confirmations[token] = pending  # restore for the rightful requester
        await query.edit_message_text("只有发起者或管理员可以确认此操作。")
        return

    if kind == "hm_cancel":
        await query.edit_message_text(f"已取消：{pending.action} {pending.target}")
        return

    await query.edit_message_text(f"正在执行：{pending.action} {pending.target} ...")
    outcome = await hm_tools.dispatch_tool_call(
        tools_settings,
        pending.action,
        pending.target,
        pending.requester_user_id,
        machine_names,
        self_host_machine,
        confirmed=True,
    )
    await context.bot.send_message(
        chat_id=pending.chat_id, text=outcome.user_message or outcome.summary
    )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args or []

    if args and args[0].lower() == "refresh":
        await list_available_models(force=True)
        args = args[1:]
        if not args:
            await update.effective_message.reply_text("模型列表已刷新。")
            # fall through to show the picker below

    models = await list_available_models()

    if args:
        requested = args[0]
        if requested not in models:
            await update.effective_message.reply_text(
                f"未知模型: {requested}\n可用: {', '.join(models)}\n"
                "如果是刚在 LiteLLM 加的模型，先发 /model refresh 再试。"
            )
            return
        chat_model_selection[chat_id] = requested
        await update.effective_message.reply_text(f"已切换到模型: {requested}")
        return

    current = get_active_model(chat_id)
    context.chat_data["model_list_snapshot"] = models
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ {name}" if name == current else name, callback_data=f"model_select:{idx}")]
            for idx, name in enumerate(models)
        ]
    )
    await update.effective_message.reply_text(
        f"当前模型：{current}\nbase_url: {settings.litellm_base_url}\n\n"
        "点按钮切换，或直接发 /model <名称>；/model refresh 刷新列表。",
        reply_markup=keyboard,
    )


async def handle_model_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if ":" not in data:
        return
    _, idx_str = data.split(":", 1)
    try:
        idx = int(idx_str)
    except ValueError:
        return

    models = context.chat_data.get("model_list_snapshot") or await list_available_models()
    if idx < 0 or idx >= len(models):
        await query.edit_message_text("该选项已过期，请重新发送 /model。")
        return

    chosen = models[idx]
    chat_model_selection[query.message.chat_id] = chosen
    await query.edit_message_text(f"已切换到模型: {chosen}")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    chat_histories.pop(chat_id, None)
    await update.effective_message.reply_text("已清空当前聊天上下文。")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    messages = [
        {"role": "system", "content": "只返回 OK。"},
        {"role": "user", "content": "健康检查"},
    ]

    try:
        await create_chat_completion_with_retries(messages, get_active_model(update.effective_chat.id))
    except APIStatusError as exc:
        await update.effective_message.reply_text(
            "模型链路异常：LiteLLM 返回错误。\n"
            f"endpoint: {settings.litellm_base_url}\n"
            f"model: {settings.litellm_model}\n"
            f"status: {exc.status_code}"
        )
        return
    except (APITimeoutError, APIConnectionError, asyncio.TimeoutError):
        await update.effective_message.reply_text(
            "模型链路异常：无法连接 LiteLLM 或请求超时。\n"
            f"endpoint: {settings.litellm_base_url}\n"
            f"model: {settings.litellm_model}"
        )
        return

    await update.effective_message.reply_text(
        "模型链路正常。\n"
        f"endpoint: {settings.litellm_base_url}\n"
        f"model: {settings.litellm_model}"
    )


def build_completion_kwargs(
    messages: list[dict[str, str]], model: str, tools: list[dict] | None = None
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "model": model,
        "messages": messages,
    }
    if settings.max_model_tokens:
        kwargs["max_tokens"] = settings.max_model_tokens
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return kwargs


def extract_answer_content(response) -> str:
    message = response.choices[0].message
    return (message.content or "").strip()


def should_retry_api_error(exc: APIStatusError) -> bool:
    return exc.status_code in RETRYABLE_STATUS_CODES


async def create_chat_completion_with_retries(
    messages: list[dict[str, str]], model: str, tools: list[dict] | None = None
):
    last_error: Exception | None = None
    attempts = settings.request_retries + 1

    for attempt in range(attempts):
        try:
            return await client.chat.completions.create(**build_completion_kwargs(messages, model, tools))
        except APIStatusError as exc:
            last_error = exc
            if not should_retry_api_error(exc) or attempt == attempts - 1:
                raise
        except (APITimeoutError, APIConnectionError, asyncio.TimeoutError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise

        delay = settings.retry_backoff_seconds * (attempt + 1)
        logger.warning(
            "LiteLLM request failed; retrying in %.1fs (%s/%s): %s",
            delay,
            attempt + 1,
            attempts - 1,
            last_error,
        )
        await asyncio.sleep(delay)

    raise RuntimeError("LiteLLM request failed after retries")


async def send_raw_tool_output(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    if not text:
        return
    for chunk in split_reply(text, settings.max_reply_chars):
        await context.bot.send_message(chat_id=chat_id, text=chunk)


async def send_confirmation_prompt(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, token: str, action: str, target: str
) -> None:
    action_zh = {"sleep": "休眠", "wake": "唤醒", "status": "查询状态"}.get(action, action)
    text = f"即将对 {target} 执行「{action_zh}」，请确认（2 分钟内有效）："
    if action == "sleep" and self_host_machine and target in (self_host_machine, "all"):
        text += (
            f"\n⚠️ 包含 {self_host_machine}（当前 LLM 推理服务所在机器），休眠后模型对话会暂时"
            f"不可用，需要用 /wake {self_host_machine} 或关键词唤醒它才能恢复。"
        )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 确认执行", callback_data=f"hm_confirm:{token}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"hm_cancel:{token}"),
            ]
        ]
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


async def run_tool_call_loop(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    telegram_user_id: int,
    messages: list[dict[str, object]],
    model: str,
) -> str | None:
    """Run a bounded LLM tool-calling loop. Returns the final assistant text, or None if a
    confirmation prompt was already sent to the chat and nothing more should be said.
    """
    for _ in range(TOOL_MAX_ROUNDS):
        response = await create_chat_completion_with_retries(messages, model, tools=[TOOL_SCHEMA])
        choice_message = response.choices[0].message
        tool_calls = choice_message.tool_calls or []

        if not tool_calls:
            return (choice_message.content or "").strip()

        messages.append(
            {
                "role": "assistant",
                "content": choice_message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            action = str(args.get("action", ""))
            target = str(args.get("target", ""))

            outcome = await hm_tools.dispatch_tool_call(
                tools_settings, action, target, telegram_user_id, machine_names, self_host_machine
            )

            if outcome.needs_confirmation:
                token = register_pending_confirmation(chat_id, telegram_user_id, action, target)
                await send_confirmation_prompt(context, chat_id, token, action, target)
                return None

            if outcome.user_message:
                await send_raw_tool_output(context, chat_id, outcome.user_message)

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": outcome.summary}
            )

    return "达到最大工具调用轮数，请重新描述你的需求。"


async def ask_litellm(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, telegram_user_id: int, prompt: str
) -> str | None:
    history = chat_histories[chat_id]
    messages = [{"role": "system", "content": f"{settings.bot_system_prompt}\n\n{TOOL_USAGE_HINT}"}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    model = get_active_model(chat_id)
    answer = await run_tool_call_loop(context, chat_id, telegram_user_id, messages, model)
    if answer is None:
        # A confirmation prompt was already sent; nothing more to say for this turn.
        return None

    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": answer})
    return answer or "模型返回了空内容，请稍后重试或增加 MAX_MODEL_TOKENS。"


async def handle_message_without_llm(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, chat_id: int, user_id: int
) -> None:
    """Deterministic keyword fallback used when the LiteLLM backend is unreachable — there's no
    model available to do tool-calling, but a wake request is exactly what's needed to fix that.
    """
    intent = hm_tools.parse_intent(prompt, machine_names)
    if intent is None:
        await update.effective_message.reply_text(
            "当前连不上 LiteLLM 模型服务（可能是 "
            f"{self_host_machine or '推理服务所在机器'} 处于休眠状态），暂时无法正常对话。\n"
            f"可以用命令直接唤醒：/wake {self_host_machine or 'all'}，"
            "或者说“唤醒 win-8”这类简单关键词，我会直接处理，不需要模型。"
        )
        return

    action, target = intent
    if not hm_tools.is_authorized(tools_settings, user_id):
        await update.effective_message.reply_text(
            "模型服务当前不可用，识别到你想执行电源操作，但你没有权限直接执行。"
        )
        return

    if action == "sleep":
        token = register_pending_confirmation(chat_id, user_id, action, target)
        await send_confirmation_prompt(context, chat_id, token, action, target)
        return

    outcome = await hm_tools.dispatch_tool_call(
        tools_settings, action, target, user_id, machine_names, self_host_machine
    )
    await reply_long_text(update, outcome.user_message or outcome.summary)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not should_answer(update, context):
        return

    prompt = extract_prompt(update, context)
    if not prompt:
        await update.effective_message.reply_text("请把问题发给我。")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await hm_tools.is_host_reachable(settings.litellm_base_url):
        await handle_message_without_llm(update, context, prompt, chat_id, user_id)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        answer = await ask_litellm(context, chat_id, user_id, prompt)
    except (APITimeoutError, APIConnectionError, asyncio.TimeoutError):
        logger.exception("LiteLLM request timed out")
        await update.effective_message.reply_text(
            "无法连接 LiteLLM 或请求模型超时，请稍后再试。\n"
            f"endpoint: {settings.litellm_base_url}\n"
            f"model: {settings.litellm_model}"
        )
        return
    except APIStatusError as exc:
        logger.exception("LiteLLM returned an API error")
        if exc.status_code == 401:
            await update.effective_message.reply_text(
                "LiteLLM 鉴权失败：请把 .env 里的 LITELLM_API_KEY 改成 LiteLLM proxy 接受的客户端 key。"
            )
            return
        if exc.status_code in {502, 503, 504}:
            await update.effective_message.reply_text(
                "模型服务暂不可用，LiteLLM 已响应，但后端模型服务可能还在启动、加载模型或暂时不可达。\n"
                f"endpoint: {settings.litellm_base_url}\n"
                f"model: {settings.litellm_model}\n"
                f"status: {exc.status_code}"
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

    if answer is None:
        return

    await reply_long_text(update, answer)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram update failed", exc_info=context.error)


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    logger.info("Telegram bot authorized as @%s", me.username)

    try:
        await create_chat_completion_with_retries(
            [
                {"role": "system", "content": "只返回 OK。"},
                {"role": "user", "content": "健康检查"},
            ],
            settings.litellm_model,
        )
        logger.info("LiteLLM health check passed")
    except Exception:
        logger.exception("LiteLLM health check failed; bot will still start")


def main() -> None:
    builder = Application.builder().token(settings.telegram_bot_token).post_init(post_init)
    if settings.telegram_proxy_url:
        builder = builder.proxy(settings.telegram_proxy_url).get_updates_proxy(settings.telegram_proxy_url)
    application = builder.build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("wake", wake_command))
    application.add_handler(CommandHandler("sleep", sleep_command))
    application.add_handler(CommandHandler("ask", handle_message))
    application.add_handler(
        CallbackQueryHandler(handle_confirmation_callback, pattern=r"^hm_(confirm|cancel):")
    )
    application.add_handler(
        CallbackQueryHandler(handle_model_selection_callback, pattern=r"^model_select:")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info(
        "Bot started. LiteLLM endpoint: %s model: %s telegram_proxy: %s",
        settings.litellm_base_url,
        settings.litellm_model,
        bool(settings.telegram_proxy_url),
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
