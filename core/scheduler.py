"""什么时候提醒 AI「可以主动找对方聊聊了」。

这里只有纯逻辑，不碰 MaiBot：输入「现在几点」和发生了什么，输出「现在要不要提醒、提醒什么」。
时间一律用时间戳（秒）；免打扰时段、「今天」按 MaiBot 所在系统的本地时间算。

三种提醒：
- idle：对方安静了一段时间（带随机浮动），受「每天最多几次」限制
- reminder：到了 AI 自己约好的时间
- followup：AI 主动说了话，对方一段时间没回，再提醒一次；追问之后对方还是没回，就等对方开口
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import random
import re
import secrets

# 提醒发出后，AI 在这段时间内说了话，才算「她主动说了」
SPEAK_WINDOW = 10 * 60
# 双方最近这段时间内说过话，说明正在聊，约定的提醒稍后再发
ACTIVE_CHAT_WINDOW = 5 * 60
# 提醒没能交给 MaiBot（例如聊天流还没准备好）时，隔多久再试
RETRY_DELAY = 10 * 60
NOTE_MAX_CHARS = 100

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


@dataclass
class Settings:
    idle_hours: float = 5.0
    jitter_percent: float = 30.0
    daily_max: int = 2
    followup_minutes: float = 30.0
    quiet_enabled: bool = True
    quiet_start: str = "00:00"
    quiet_end: str = "09:00"
    schedule_enabled: bool = True
    schedule_min_minutes: float = 10.0
    schedule_max_days: float = 7.0
    schedule_max_pending: int = 3


@dataclass
class Reminder:
    id: str
    due_at: float
    note: str
    created_at: float


@dataclass
class Trigger:
    kind: str  # idle / reminder / followup
    idle_seconds: float = 0.0
    reminder: Optional[Reminder] = None


@dataclass
class State:
    # 设定的聊天对象（平台:账号）和认出来的 MaiBot 聊天流
    target: str = ""
    stream_id: str = ""
    last_inbound_at: Optional[float] = None
    last_outbound_at: Optional[float] = None
    idle_due_at: Optional[float] = None
    day: str = ""
    triggers_today: int = 0
    # 刚发出的提醒：{"kind": ..., "at": 时间戳}，等 AI 在 SPEAK_WINDOW 内开口
    awaiting: Optional[Dict[str, Any]] = None
    followup_due_at: Optional[float] = None
    # 追问过了对方还没回：在对方开口前不再按「安静」提醒
    paused: bool = False
    reminders: List[Reminder] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "State":
        if not isinstance(data, dict):
            return cls()
        state = cls()
        for key in ("target", "stream_id", "day"):
            if isinstance(data.get(key), str):
                setattr(state, key, data[key])
        for key in ("last_inbound_at", "last_outbound_at", "idle_due_at", "followup_due_at"):
            if isinstance(data.get(key), (int, float)):
                setattr(state, key, float(data[key]))
        if isinstance(data.get("triggers_today"), int):
            state.triggers_today = data["triggers_today"]
        if isinstance(data.get("awaiting"), dict):
            state.awaiting = data["awaiting"]
        state.paused = bool(data.get("paused", False))
        for item in data.get("reminders") or []:
            try:
                state.reminders.append(
                    Reminder(str(item["id"]), float(item["due_at"]), str(item["note"]), float(item["created_at"]))
                )
            except (KeyError, TypeError, ValueError):
                continue
        return state


def parse_hhmm(text: str) -> Optional[int]:
    """「09:30」→ 一天中的第几分钟；格式不对返回 None。"""
    match = _HHMM.match(str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def in_quiet_hours(ts: float, settings: Settings) -> bool:
    if not settings.quiet_enabled:
        return False
    start, end = parse_hhmm(settings.quiet_start), parse_hhmm(settings.quiet_end)
    if start is None or end is None or start == end:
        return False
    moment = _local(ts)
    minute = moment.hour * 60 + moment.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # 跨午夜，例如 23:00–08:00


def quiet_end_after(ts: float, settings: Settings) -> float:
    """ts 在免打扰时段里时，返回这段免打扰结束的时间；不在时原样返回。"""
    if not in_quiet_hours(ts, settings):
        return ts
    end = parse_hhmm(settings.quiet_end) or 0
    moment = _local(ts)
    candidate = moment.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def describe_duration(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.0f} 个小时" if hours >= 3 else f"{hours:.1f} 个小时".replace(".0 ", " ")
    return f"{hours / 24:.0f} 天"


class Scheduler:
    """持有状态，并按设置决定什么时候提醒。所有方法都不阻塞。"""

    def __init__(self, state: State, settings: Settings, rng: Optional[random.Random] = None) -> None:
        self.state = state
        self.settings = settings
        self._rng = rng or random.Random()
        self.on_change: Callable[[], None] = lambda: None

    # ── 发生了什么 ────────────────────────────────

    def note_inbound(self, now: float) -> None:
        """对方说话了：一切重新开始计时。"""
        s = self.state
        s.last_inbound_at = now
        s.awaiting = None
        s.followup_due_at = None
        s.paused = False
        s.idle_due_at = self._next_idle(now)
        self.on_change()

    def note_outbound(self, now: float) -> None:
        """AI 给对方发了消息。"""
        s = self.state
        s.last_outbound_at = now
        awaiting = s.awaiting
        if awaiting and now - float(awaiting.get("at", 0)) <= SPEAK_WINDOW:
            s.awaiting = None
            if awaiting.get("kind") == "followup" or self.settings.followup_minutes <= 0:
                s.followup_due_at = None
                s.paused = True
            else:
                s.followup_due_at = now + self.settings.followup_minutes * 60
            s.idle_due_at = None
        elif s.followup_due_at is None and not s.paused:
            # 平常聊天中的回复：从这句话起重新计算安静时间
            s.idle_due_at = self._next_idle(now)
        self.on_change()

    def start_if_unknown(self, now: float) -> None:
        """刚装上、还不知道上次聊天是什么时候：从现在开始计时。"""
        if self.state.idle_due_at is None and self.state.awaiting is None and self.state.followup_due_at is None:
            if not self.state.paused:
                self.state.idle_due_at = self._next_idle(now)
                self.on_change()

    # ── AI 自己约的提醒 ────────────────────────────

    def add_reminder(self, now: float, due_at: float, note: str) -> Reminder:
        """校验时间范围和数量，超出时抛 ValueError（消息直接给 AI 看）。"""
        cfg = self.settings
        note = " ".join(str(note or "").split())[:NOTE_MAX_CHARS]
        if not note:
            raise ValueError("请写一句备注，说明到时候想找对方聊什么")
        earliest = now + cfg.schedule_min_minutes * 60
        latest = now + cfg.schedule_max_days * 86400
        if due_at < earliest - 1:
            raise ValueError(f"最早只能约 {describe_duration(cfg.schedule_min_minutes * 60)}以后")
        if due_at > latest + 1:
            raise ValueError(f"最晚只能约 {describe_duration(cfg.schedule_max_days * 86400)}以内")
        if len(self.state.reminders) >= cfg.schedule_max_pending:
            raise ValueError(f"已经约了 {len(self.state.reminders)} 个，最多 {cfg.schedule_max_pending} 个；可以先取消一个")
        reminder = Reminder(id=secrets.token_hex(3), due_at=due_at, note=note, created_at=now)
        self.state.reminders.append(reminder)
        self.state.reminders.sort(key=lambda r: r.due_at)
        # 已经约了时间，就不必在那之前再按「安静」提醒一次
        if self.state.idle_due_at is not None and self.state.idle_due_at < due_at:
            self.state.idle_due_at = due_at + self._idle_seconds()
        self.on_change()
        return reminder

    def cancel_reminder(self, reminder_id: str) -> Optional[Reminder]:
        for reminder in self.state.reminders:
            if reminder.id == reminder_id.strip():
                self.state.reminders.remove(reminder)
                self.on_change()
                return reminder
        return None

    # ── 到点检查 ──────────────────────────────────

    def tick(self, now: float) -> Optional[Trigger]:
        """每隔一会调用一次；需要提醒 AI 时返回提醒内容，并先把状态改好（不会重复提醒）。"""
        s, cfg = self.state, self.settings
        changed = self._roll_day(now)

        if s.awaiting is not None:
            if now - float(s.awaiting.get("at", 0)) <= SPEAK_WINDOW:
                if changed:
                    self.on_change()
                return None
            # 提醒过了，她选择不说：过一段时间再看
            kind = s.awaiting.get("kind")
            s.awaiting = None
            if kind == "followup":
                s.paused = True
            elif not s.paused and s.followup_due_at is None:
                s.idle_due_at = self._next_idle(now)
            changed = True

        quiet = in_quiet_hours(now, cfg)
        trigger: Optional[Trigger] = None

        if s.followup_due_at is not None and now >= s.followup_due_at:
            s.followup_due_at = None
            if quiet:
                s.paused = True  # 追问落在免打扰时段：不追问，等对方开口
            else:
                trigger = Trigger("followup", idle_seconds=now - (s.last_outbound_at or now))
            changed = True
        elif not quiet:
            due = next((r for r in s.reminders if r.due_at <= now), None)
            if due is not None and cfg.schedule_enabled and not self._chat_active(now):
                # AI 自己约的提醒不受每天次数限制（数量另有上限）
                s.reminders.remove(due)
                trigger = Trigger("reminder", reminder=due)
            elif (
                s.idle_due_at is not None
                and now >= s.idle_due_at
                and not s.paused
                and s.followup_due_at is None
                and s.triggers_today < cfg.daily_max
            ):
                last = max(s.last_inbound_at or 0, s.last_outbound_at or 0)
                trigger = Trigger("idle", idle_seconds=now - last if last else 0)
                s.idle_due_at = None
                s.triggers_today += 1

        if trigger is not None:
            s.awaiting = {"kind": trigger.kind, "at": now}
            changed = True
        if changed:
            self.on_change()
        return trigger

    def trigger_failed(self, trigger: Trigger, now: float) -> None:
        """提醒没交给 MaiBot：撤回计数，稍后再试。"""
        s = self.state
        s.awaiting = None
        if trigger.kind == "reminder" and trigger.reminder is not None:
            trigger.reminder.due_at = now + RETRY_DELAY
            s.reminders.append(trigger.reminder)
            s.reminders.sort(key=lambda r: r.due_at)
        elif trigger.kind == "idle":
            s.idle_due_at = now + RETRY_DELAY
            s.triggers_today = max(0, s.triggers_today - 1)
        elif trigger.kind == "followup":
            s.followup_due_at = now + RETRY_DELAY
        self.on_change()

    # ── 内部 ──────────────────────────────────────

    def _idle_seconds(self) -> float:
        base = max(0.25, self.settings.idle_hours) * 3600
        jitter = max(0.0, min(90.0, self.settings.jitter_percent)) / 100
        return base * (1 + self._rng.uniform(-jitter, jitter))

    def _next_idle(self, now: float) -> float:
        return now + self._idle_seconds()

    def _chat_active(self, now: float) -> bool:
        last = max(self.state.last_inbound_at or 0, self.state.last_outbound_at or 0)
        return bool(last) and now - last < ACTIVE_CHAT_WINDOW

    def _roll_day(self, now: float) -> bool:
        today = _local(now).strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day = today
            self.state.triggers_today = 0
            return True
        return False


def parse_when(text: str, now: float) -> Optional[float]:
    """AI 填的时间：「2026-10-17 18:00」或「18:00」（今天已过就是明天）。认不出返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    year = _local(now).year
    for fmt, with_year in (("%Y-%m-%d %H:%M", False), ("%Y/%m/%d %H:%M", False), ("%Y-%m-%d %H:%M", True), ("%Y/%m/%d %H:%M", True)):
        try:
            parsed = datetime.strptime(f"{year}{fmt[2]}{raw}" if with_year else raw, fmt)
        except ValueError:
            continue
        if with_year and parsed.timestamp() < now - 60:
            parsed = parsed.replace(year=year + 1)  # 「1-05 09:00」在年底说就是明年
        return parsed.timestamp()
    minute = parse_hhmm(raw)
    if minute is None:
        return None
    base = _local(now)
    candidate = base.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def format_time(ts: float, now: float) -> str:
    """给人和 AI 看的时间：今天 18:00 / 明天 09:30 / 10月20日 18:00。"""
    moment, today = _local(ts), _local(now).date()
    days = (moment.date() - today).days
    prefix = {-2: "前天", -1: "昨天", 0: "今天", 1: "明天", 2: "后天"}.get(days, f"{moment.month}月{moment.day}日")
    return f"{prefix} {moment:%H:%M}"
