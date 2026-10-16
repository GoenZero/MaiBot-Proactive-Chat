"""MaiBot 主动聊天插件。

聊天安静一段时间、到了 AI 自己约好的时间、或者 AI 主动说了话对方没回时，
用 MaiBot 自带的「主动任务」提醒 AI 一下。说不说、说什么，由 AI 自己决定。

插件不自己调模型、不直接发消息、不编造任何信息。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

import asyncio
import contextlib
import json
import os
import time

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import PLUGIN_VERSION, ProactiveChatConfig
from .core.intents import SCHEDULE_TOOL, build_intent
from .core.scheduler import Scheduler, Settings, State, Trigger, format_time, in_quiet_hours, parse_hhmm, parse_when

CANCEL_TOOL = "proactive_cancel"
OUR_TOOLS = {SCHEDULE_TOOL, CANCEL_TOOL}
# 多久检查一次要不要提醒
TICK_SECONDS = 30.0

SCHEDULE_DESCRIPTION = (
    "约个时间再主动找对方聊，到点会提醒你。适合：对方提到之后要做的事（面试、考试、出门、看病），想到时候问问结果；"
    "话题没聊完想改天接着聊；收到「可以主动找对方聊聊」的提醒但觉得现在不合适，想晚点再说。"
    "minutes_later（几分钟以后）和 at（具体时间，例如 18:00 或 2026-10-20 18:00）填一个。"
    "note 写一句给自己的备注，到时候会原样提醒你，例如「问问面试结果」。不要每轮都约，真有想接着聊的事再约。"
)
CANCEL_DESCRIPTION = "取消一个你之前约好的「再找对方聊」的提醒，例如对方已经主动说了那件事。reminder_id 在约的时候会告诉你。"


def normalize_account(raw: str) -> str:
    """号码去掉空格、横线、括号（"+81 80-1234-5678" → "+818012345678"）；邮箱只转小写。"""
    value = str(raw or "").strip()
    if "@" in value or not any(ch.isdigit() for ch in value):
        return value.lower()
    digits = "".join(ch for ch in value if ch.isdigit())
    return ("+" if value.startswith("+") else "") + digits


class ProactiveChatPlugin(MaiBotPlugin):
    config_model: ClassVar[type] = ProactiveChatConfig

    def __init__(self) -> None:
        super().__init__()
        self._scheduler: Optional[Scheduler] = None
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._target_key = ""

    # ── 生命周期 ──────────────────────────────────

    async def on_load(self) -> None:
        state = self._load_state()
        self._target_key = self._current_target_key()
        if state.target != self._target_key:
            # 第一次运行，或者停用期间换了聊天对象
            state = State(target=self._target_key)
        self._scheduler = Scheduler(state, self._settings())
        self._scheduler.on_change = self._save_state
        self._loop_task = asyncio.create_task(self._run())
        self.ctx.logger.info("主动聊天插件已加载（v%s）", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._save_state()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        scheduler = self._scheduler
        if scheduler is None:
            return
        scheduler.settings = self._settings()
        key = self._current_target_key()
        if key != self._target_key:
            # 换了聊天对象：等新对象先发一条消息再开始
            self._target_key = key
            scheduler.state = State(target=key)
            self._save_state()
        self.ctx.logger.info("主动聊天插件配置已生效")

    # ── 配置与状态 ────────────────────────────────

    def _settings(self) -> Settings:
        cfg = self.config
        return Settings(
            idle_hours=cfg.timing.idle_hours,
            jitter_percent=cfg.timing.jitter_percent,
            daily_max=cfg.timing.daily_max,
            followup_minutes=cfg.timing.followup_minutes,
            quiet_enabled=cfg.quiet.enabled,
            quiet_start=cfg.quiet.start,
            quiet_end=cfg.quiet.end,
            schedule_enabled=cfg.schedule.enabled,
            schedule_min_minutes=cfg.schedule.min_minutes,
            schedule_max_days=cfg.schedule.max_days,
            schedule_max_pending=cfg.schedule.max_pending,
        )

    def _current_target_key(self) -> str:
        user = normalize_account(self.config.target.user_id)
        platform = self.config.target.platform.strip().lower()
        return f"{platform}:{user}" if user and platform else ""

    def _state_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "state.json"

    def _load_state(self) -> State:
        path = self._state_path()
        try:
            return State.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return State()
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("状态文件读取失败，重新开始计时：%s", exc)
            return State()

    def _save_state(self) -> None:
        scheduler = self._scheduler
        if scheduler is None:
            return
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(scheduler.state.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temp, path)
        except OSError as exc:
            self.ctx.logger.warning("状态文件保存失败：%s", exc)

    def _active(self) -> bool:
        return bool(self.config.plugin.enabled and self._target_key)

    def _is_target_message(self, message: Any) -> bool:
        """这条消息是不是和设定对象的私聊（看平台和对方账号）。"""
        if not isinstance(message, dict) or not self._target_key:
            return False
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        if info.get("group_info"):
            return False
        user = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        key = f"{str(message.get('platform', '')).strip().lower()}:{normalize_account(str(user.get('user_id', '')))}"
        return key == self._target_key

    def _is_target_outbound(self, message: Any) -> bool:
        if not isinstance(message, dict) or self._scheduler is None:
            return False
        stream_id = self._scheduler.state.stream_id
        if stream_id and str(message.get("session_id", "")) == stream_id:
            return True
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        extra = info.get("additional_config") if isinstance(info.get("additional_config"), dict) else {}
        target = normalize_account(str(extra.get("platform_io_target_user_id", "")))
        key = f"{str(message.get('platform', '')).strip().lower()}:{target}"
        return bool(target) and not info.get("group_info") and key == self._target_key

    # ── 观察收发 ──────────────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="proactive_note_inbound",
        description="记下对方最近一次说话的时间（只看，不改消息）",
        mode=HookMode.OBSERVE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_inbound(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        scheduler = self._scheduler
        if scheduler is None or not self._is_target_message(message):
            return None
        session_id = str(message.get("session_id") or "")
        if session_id and session_id != scheduler.state.stream_id:
            scheduler.state.stream_id = session_id
            self.ctx.logger.info("主动聊天：已认出和 %s 的聊天", self._target_key)
        scheduler.note_inbound(time.time())
        return None

    @HookHandler(
        "send_service.after_send",
        name="proactive_note_outbound",
        description="记下 AI 最近一次给对方发消息的时间（只看，不改消息）",
        mode=HookMode.OBSERVE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_outbound(self, **kwargs: Any) -> None:
        scheduler = self._scheduler
        if scheduler is None or not kwargs.get("sent") or not self._is_target_outbound(kwargs.get("message")):
            return None
        # 不存进聊天记录的是命令回复、系统提示，不是 AI 说的话（AI 的回复都会存）
        if kwargs.get("storage_message") is False:
            return None
        scheduler.note_outbound(time.time())
        return None

    @HookHandler(
        "maisaka.planner.before_request",
        name="proactive_tool_visibility",
        description="约时间的工具只在设定对象的私聊里给 AI 看",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_request(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        tools = kwargs.get("tool_definitions")
        if not isinstance(tools, list):
            return None
        if self._schedule_allowed(str(kwargs.get("session_id") or "")):
            return None
        kept = [t for t in tools if not (isinstance(t, dict) and _tool_name(t) in OUR_TOOLS)]
        if len(kept) == len(tools):
            return None
        return {"action": "continue", "modified_kwargs": {**kwargs, "tool_definitions": kept}}

    def _schedule_allowed(self, session_id: str) -> bool:
        scheduler = self._scheduler
        return bool(
            scheduler is not None
            and self._active()
            and self.config.schedule.enabled
            and session_id
            and session_id == scheduler.state.stream_id
        )

    # ── AI 自己约时间 ─────────────────────────────

    @Tool(
        SCHEDULE_TOOL,
        description=SCHEDULE_DESCRIPTION,
        parameters=[
            ToolParameterInfo(name="note", param_type=ToolParamType.STRING, description="给自己的备注：到时候想找对方聊什么"),
            ToolParameterInfo(
                name="minutes_later", param_type=ToolParamType.INTEGER, description="几分钟以后提醒你", required=False
            ),
            ToolParameterInfo(
                name="at", param_type=ToolParamType.STRING, description="具体时间，例如 18:00 或 2026-10-20 18:00", required=False
            ),
        ],
        chat_scope="private",
    )
    async def tool_schedule(
        self, note: str = "", minutes_later: Any = None, at: str = "", stream_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        del kwargs
        scheduler = self._scheduler
        if scheduler is None or not self._schedule_allowed(stream_id):
            return {"success": False, "error": "这个聊天里不能约时间"}
        now = time.time()
        due_at: Optional[float] = None
        if minutes_later not in (None, ""):
            try:
                due_at = now + float(minutes_later) * 60
            except (TypeError, ValueError):
                return {"success": False, "error": "minutes_later 要填数字"}
        elif at:
            due_at = parse_when(at, now)
            if due_at is None:
                return {"success": False, "error": "at 的格式认不出，请写成 18:00 或 2026-10-20 18:00"}
        else:
            return {"success": False, "error": "minutes_later 和 at 要填一个"}
        try:
            reminder = scheduler.add_reminder(now, due_at, note)
        except ValueError as exc:
            return {"success": False, "error": f"{exc}。{self._pending_text(now)}"}
        content = f"约好了：{format_time(reminder.due_at, now)}提醒你「{reminder.note}」（编号 {reminder.id}）。"
        if in_quiet_hours(reminder.due_at, scheduler.settings):
            content += f"这个时间在免打扰时段里，会等到 {scheduler.settings.quiet_end} 以后再提醒。"
        return {"success": True, "content": content}

    @Tool(
        CANCEL_TOOL,
        description=CANCEL_DESCRIPTION,
        parameters=[ToolParameterInfo(name="reminder_id", param_type=ToolParamType.STRING, description="约的时候告诉你的编号")],
        chat_scope="private",
    )
    async def tool_cancel(self, reminder_id: str = "", stream_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        scheduler = self._scheduler
        if scheduler is None or not self._schedule_allowed(stream_id):
            return {"success": False, "error": "这个聊天里没有约定"}
        removed = scheduler.cancel_reminder(str(reminder_id or ""))
        if removed is None:
            return {"success": False, "error": f"没找到这个编号。{self._pending_text(time.time())}"}
        return {"success": True, "content": f"已取消「{removed.note}」这个约定"}

    def _pending_text(self, now: float) -> str:
        reminders = self._scheduler.state.reminders if self._scheduler else []
        if not reminders:
            return "现在没有约定。"
        items = "；".join(f"{r.id}：{format_time(r.due_at, now)}「{r.note}」" for r in reminders)
        return f"现在的约定：{items}"

    # ── 定时检查 ──────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 后台循环不能因为一次出错就停
                self.ctx.logger.exception("主动聊天：检查时出错")
            await asyncio.sleep(TICK_SECONDS)

    async def check_once(self, now: float) -> Optional[Trigger]:
        """检查一次；需要时提醒 AI。返回这次发出的提醒（测试用）。"""
        scheduler = self._scheduler
        if scheduler is None or not self._active() or not scheduler.state.stream_id:
            return None
        scheduler.start_if_unknown(now)
        trigger = scheduler.tick(now)
        if trigger is None:
            return None
        intent = build_intent(
            trigger,
            now,
            can_schedule=self.config.schedule.enabled,
            followup_minutes=scheduler.settings.followup_minutes,
        )
        try:
            result = await self.ctx.maisaka.trigger_proactive(scheduler.state.stream_id, intent)
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "error": str(exc)}
        if isinstance(result, dict) and result.get("success") is False:
            self.ctx.logger.warning("主动聊天：提醒没交给 MaiBot（%s），稍后再试", result.get("error"))
            scheduler.trigger_failed(trigger, now)
            return None
        self.ctx.logger.info("主动聊天：已提醒 AI（%s）", {"idle": "安静太久", "reminder": "到了约好的时间", "followup": "追问"}[trigger.kind])
        return trigger

    # ── 状态命令 ──────────────────────────────────

    @Command("proactive_status", description="查看主动聊天插件的状态", pattern=r"^/proactive_status$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        # 不存进聊天记录：否则 MaiBot 重启后恢复上下文、生成聊天回想和长期记忆时，会把状态当成 AI 说过的话
        text = self.status_text(time.time(), show_notes=self._schedule_allowed(stream_id))
        await self.ctx.send.text(text, stream_id, storage_message=False)
        return True, "状态已显示", True

    def status_text(self, now: float, *, show_notes: bool) -> str:
        cfg = self.config
        scheduler = self._scheduler
        lines = [f"💬 主动聊天 v{PLUGIN_VERSION}", f"系统时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}"]
        if not cfg.plugin.enabled:
            lines.append("状态：已关闭")
        elif not self._target_key:
            lines.append("状态：还没填聊天对象")
        elif scheduler is None or not scheduler.state.stream_id:
            lines.append(f"状态：等 {self._target_key} 先发一条消息")
        else:
            s = scheduler.state
            lines.append(f"聊天对象：{self._target_key}")
            if in_quiet_hours(now, scheduler.settings):
                lines.append(f"现在是免打扰时段（{cfg.quiet.start}–{cfg.quiet.end}）")
            if s.awaiting:
                lines.append("刚提醒过 AI，等她决定")
            elif s.followup_due_at:
                lines.append(f"AI 主动说了话，{format_time(s.followup_due_at, now)}还没回复就提醒她追问")
            elif s.paused:
                lines.append("追问过了对方还没回：等对方开口后再重新计时")
            elif s.idle_due_at:
                lines.append(f"下次「安静太久」提醒：大约 {format_time(s.idle_due_at, now)}")
            lines.append(f"今天已提醒：{s.triggers_today} / {cfg.timing.daily_max} 次")
            if s.reminders:
                lines.append(f"AI 约好的提醒（{len(s.reminders)} 个）：")
                for r in s.reminders:
                    lines.append(f"  {format_time(r.due_at, now)}" + (f"「{r.note}」" if show_notes else ""))
        for name, value in (("开始", cfg.quiet.start), ("结束", cfg.quiet.end)):
            if cfg.quiet.enabled and parse_hhmm(value) is None:
                lines.append(f"⚠️ 免打扰{name}时间「{value}」格式不对，免打扰暂时不生效")
        return "\n".join(lines)


def _tool_name(definition: Dict[str, Any]) -> str:
    function = definition.get("function")
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(definition.get("name", ""))


def create_plugin() -> ProactiveChatPlugin:
    """MaiBot 插件工厂函数。"""
    return ProactiveChatPlugin()
