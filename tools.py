"""Home-machine power control tool, invokable either as an LLM function-call or a deterministic
Telegram command. Both paths share dispatch_tool_call() so auth and confirmation rules only
live in one place.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger("telegram-litellm-bot.tools")

ACTIONS = ("wake", "sleep", "status")

# Fallback keyword intent, used only when the LiteLLM backend itself is unreachable (e.g. its
# host machine is asleep) — there is no model available to do tool-calling in that state, so a
# natural-language wake request has to be recognized without one.
_ACTION_KEYWORDS = {
    "wake": ("唤醒", "叫醒", "开机", "打开电脑", "wake", "power on", "turn on"),
    "sleep": ("休眠", "睡眠", "关机", "睡了", "sleep", "suspend", "hibernate", "shutdown", "turn off"),
    "status": ("状态", "在线吗", "在不在", "status", "online"),
}
_ALL_KEYWORDS = ("all", "全部", "所有", "全都", "都")


@dataclass(frozen=True)
class ToolsSettings:
    hm_script_path: str
    hm_python_bin: str
    hm_command_timeout_seconds: float
    machines_config_path: str
    admin_user_ids: frozenset[int]


@dataclass
class CommandResult:
    ok: bool
    output: str
    error: str | None = None


@dataclass
class ToolCallOutcome:
    ok: bool
    summary: str  # compact text fed back to the LLM as the tool result
    user_message: str | None = None  # raw hm output, sent to the Telegram user directly
    needs_confirmation: bool = False


def load_machine_names(machines_config_path: str) -> list[str]:
    try:
        with open(machines_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return sorted(data.get("machines", {}).keys())
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load machines config at %s", machines_config_path)
        return []


def resolve_self_host_machine(machines_config_path: str, litellm_base_url: str) -> str | None:
    """Return the machine name whose IP matches the LiteLLM backend host, if any.

    Used only to add a heads-up note when this machine is targeted — sleeping it makes the
    LiteLLM backend unreachable until it's woken again, so LLM tool-calling won't work in the
    meantime (see is_host_reachable() / parse_intent() for the deterministic fallback).
    """
    host = urlparse(litellm_base_url).hostname
    if not host:
        return None
    try:
        with open(machines_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    for name, cfg in data.get("machines", {}).items():
        if cfg.get("ip") == host:
            return name
    return None


async def is_host_reachable(base_url: str, timeout: float = 2.0) -> bool:
    """Quick TCP reachability check for the LiteLLM backend host:port.

    Used to decide routing: if this fails, there is no point sending a chat completion
    request (it would just time out), so natural-language power commands fall back to a
    deterministic keyword parse instead of LLM tool-calling.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


def _normalize(text: str) -> str:
    return re.sub(r"[-_\s]", "", text).lower()


def parse_intent(text: str, machine_names: list[str]) -> tuple[str, str] | None:
    """Best-effort deterministic action+target parse from free text.

    Only used as a fallback when the LLM backend is unreachable. Deliberately conservative:
    returns None (no confident match) rather than guessing a target, since a false positive
    on 'sleep' would put up a real confirmation prompt for something the user didn't mean.
    """
    normalized_text = _normalize(text)

    action = None
    for candidate_action, keywords in _ACTION_KEYWORDS.items():
        if any(_normalize(k) in normalized_text for k in keywords):
            action = candidate_action
            break
    if action is None:
        return None

    if any(_normalize(k) in normalized_text for k in _ALL_KEYWORDS):
        return action, "all"

    for name in machine_names:
        if _normalize(name) in normalized_text:
            return action, name

    return None


def build_tool_schema(machine_names: list[str]) -> dict:
    targets = ["all", *machine_names]
    return {
        "type": "function",
        "function": {
            "name": "home_machine_control",
            "description": (
                "Wake (power on via Wake-on-LAN), sleep (suspend/hibernate), or check the "
                "online status of home machines. Only call this when the user explicitly "
                "asks to turn on/wake up, turn off/sleep/suspend, or check a machine (or "
                "'all' machines). Do not call it for unrelated chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(ACTIONS),
                        "description": "wake = power on via WOL, sleep = suspend/hibernate, status = check online state",
                    },
                    "target": {
                        "type": "string",
                        "enum": targets,
                        "description": "Machine name, or 'all' for every machine",
                    },
                },
                "required": ["action", "target"],
            },
        },
    }


def is_authorized(settings: ToolsSettings, telegram_user_id: int) -> bool:
    # Fail closed: no admins configured means the feature is effectively disabled.
    return telegram_user_id in settings.admin_user_ids


def _validate_target(target: str, machine_names: list[str]) -> str | None:
    if target != "all" and target not in machine_names:
        return f"未知机器: {target}（可用: all, {', '.join(machine_names)}）"
    return None


async def run_hm_command(settings: ToolsSettings, action: str, target: str) -> CommandResult:
    cmd = [settings.hm_python_bin, settings.hm_script_path, action, target]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return CommandResult(ok=False, output="", error=f"无法启动 hm 脚本: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.hm_command_timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CommandResult(
            ok=False, output="", error=f"命令超时（>{settings.hm_command_timeout_seconds:.0f}s）"
        )

    text_out = stdout.decode("utf-8", errors="replace").strip()
    text_err = stderr.decode("utf-8", errors="replace").strip()
    return CommandResult(ok=proc.returncode == 0, output=text_out, error=text_err or None)


async def dispatch_tool_call(
    settings: ToolsSettings,
    action: str,
    target: str,
    telegram_user_id: int,
    machine_names: list[str],
    self_host_machine: str | None,
    confirmed: bool = False,
) -> ToolCallOutcome:
    logger.info(
        "tool_call action=%r target=%r user=%s confirmed=%s",
        action, target, telegram_user_id, confirmed,
    )

    if action not in ACTIONS:
        return ToolCallOutcome(ok=False, summary=f"未知操作: {action}")

    if not is_authorized(settings, telegram_user_id):
        logger.warning(
            "tool_call REJECTED (unauthorized) action=%s target=%s user=%s",
            action, target, telegram_user_id,
        )
        return ToolCallOutcome(ok=False, summary="拒绝执行：当前用户没有控制家庭设备电源的权限。")

    target_error = _validate_target(target, machine_names)
    if target_error:
        return ToolCallOutcome(ok=False, summary=target_error)

    if action == "sleep" and not confirmed:
        logger.info(
            "tool_call PENDING_CONFIRMATION action=%s target=%s user=%s", action, target, telegram_user_id
        )
        return ToolCallOutcome(ok=True, summary="该操作需要用户二次确认后才会执行。", needs_confirmation=True)

    # Let hm's own resolve_targets() expand "all" and handle jump-host sleep ordering — that
    # logic (sleep dependents before the jump host they tunnel through) already lives there.
    result = await run_hm_command(settings, action, target)
    logger.info(
        "hm_command action=%s target=%s ok=%s output=%r error=%r",
        action, target, result.ok, result.output[:500], result.error,
    )

    user_message = result.output or result.error or ""
    hits_self_host = self_host_machine and (target == self_host_machine or target == "all")
    if action == "sleep" and hits_self_host and self_host_machine in machine_names:
        user_message += (
            f"\n\n提示：{self_host_machine} 是当前 LLM 推理服务所在机器，休眠后模型对话会暂时不可用，"
            f"可以用 /wake {self_host_machine} 或直接说“唤醒 {self_host_machine}”重新唤醒它。"
        )

    summary = json.dumps(
        {"action": action, "target": target, "ok": result.ok}, ensure_ascii=False
    )
    return ToolCallOutcome(ok=result.ok, summary=summary, user_message=user_message)
