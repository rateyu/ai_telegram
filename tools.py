"""Home-machine power control tool, invokable either as an LLM function-call or a deterministic
Telegram command. Both paths share dispatch_tool_call() so auth, self-host protection and
confirmation rules only live in one place.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger("telegram-litellm-bot.tools")

ACTIONS = ("wake", "sleep", "status")


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

    Waking/sleeping this machine through itself is unsafe: a sleep command can cut the
    connection the bot's own model inference depends on mid-request.
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

    if action == "sleep" and target == self_host_machine:
        logger.warning(
            "tool_call REJECTED (self-host sleep) target=%s user=%s", target, telegram_user_id
        )
        return ToolCallOutcome(
            ok=False,
            summary=(
                f"拒绝执行：{target} 是当前 LLM 推理服务所在的机器，"
                "通过它自己下发睡眠指令有掐断当前对话链路的风险，请手动处理。"
            ),
        )

    if action == "sleep" and not confirmed:
        logger.info("tool_call PENDING_CONFIRMATION action=%s target=%s user=%s", action, target, telegram_user_id)
        return ToolCallOutcome(ok=True, summary="该操作需要用户二次确认后才会执行。", needs_confirmation=True)

    excluded_self_host = False
    if action == "sleep" and target == "all" and self_host_machine in machine_names:
        run_targets = [m for m in machine_names if m != self_host_machine]
        excluded_self_host = True
    else:
        run_targets = [target]

    lines = []
    all_ok = True
    for t in run_targets:
        result = await run_hm_command(settings, action, t)
        all_ok = all_ok and result.ok
        logger.info(
            "hm_command action=%s target=%s ok=%s output=%r error=%r",
            action, t, result.ok, result.output[:500], result.error,
        )
        status = "成功" if result.ok else "失败"
        body = result.output or result.error or ""
        lines.append(f"{t}: {status}\n{body}".strip())

    if excluded_self_host:
        lines.append(f"{self_host_machine}: 已跳过（LLM 推理服务所在机器，需手动处理）")

    raw_output = "\n\n".join(lines)
    summary = json.dumps(
        {
            "action": action,
            "requested_target": target,
            "executed_targets": run_targets,
            "excluded_self_host": excluded_self_host,
            "ok": all_ok,
        },
        ensure_ascii=False,
    )

    return ToolCallOutcome(ok=all_ok, summary=summary, user_message=raw_output)
