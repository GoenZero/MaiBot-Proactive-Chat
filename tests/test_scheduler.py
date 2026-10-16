from __future__ import annotations

from datetime import datetime

import random

import pytest

from proactive_chat_under_test.core.intents import build_intent
from proactive_chat_under_test.core.scheduler import (
    SPEAK_WINDOW,
    Scheduler,
    Settings,
    State,
    Trigger,
    format_time,
    in_quiet_hours,
    parse_when,
    quiet_end_after,
)

HOUR = 3600.0


def at(hour: int, minute: int = 0, day: int = 16) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def make(**overrides) -> Scheduler:
    settings = Settings(jitter_percent=0, **overrides)
    return Scheduler(State(stream_id="s1"), settings, rng=random.Random(1))


def test_quiet_hours_including_across_midnight():
    day = Settings(quiet_start="00:00", quiet_end="09:00")
    assert in_quiet_hours(at(3), day) and not in_quiet_hours(at(9), day) and not in_quiet_hours(at(23, 59), day)
    night = Settings(quiet_start="23:00", quiet_end="08:00")
    assert in_quiet_hours(at(23, 30), night) and in_quiet_hours(at(7, 59), night) and not in_quiet_hours(at(12), night)
    assert quiet_end_after(at(23, 30), night) == at(8, 0, day=17)
    # 格式不对、开始等于结束、关掉：都不算免打扰
    assert not in_quiet_hours(at(3), Settings(quiet_start="半夜", quiet_end="09:00"))
    assert not in_quiet_hours(at(3), Settings(quiet_start="09:00", quiet_end="09:00"))
    assert not in_quiet_hours(at(3), Settings(quiet_enabled=False))


def test_idle_reminder_fires_after_silence_and_counts_per_day():
    s = make(idle_hours=5, daily_max=1)
    s.note_inbound(at(10))
    assert s.tick(at(14, 59)) is None
    trigger = s.tick(at(15, 1))
    assert trigger is not None and trigger.kind == "idle"
    assert abs(trigger.idle_seconds - (5 * HOUR + 60)) < 1
    # 她没开口：过一会重新计时，但今天次数已用完
    assert s.tick(at(15, 1) + SPEAK_WINDOW + 1) is None
    assert s.state.idle_due_at is not None
    assert s.tick(at(23)) is None
    # 第二天（免打扰结束后）可以再提醒
    assert s.tick(at(10, 0, day=17)).kind == "idle"


def test_jitter_stays_within_range():
    for seed in range(50):
        s = Scheduler(State(stream_id="s1"), Settings(idle_hours=5, jitter_percent=30), rng=random.Random(seed))
        s.note_inbound(at(10))
        delay = (s.state.idle_due_at - at(10)) / HOUR
        assert 3.5 - 1e-6 <= delay <= 6.5 + 1e-6


def test_no_idle_reminder_during_quiet_hours():
    s = make(idle_hours=1)
    s.note_inbound(at(23, 30))
    assert s.tick(at(2, 0, day=17)) is None
    assert s.tick(at(9, 1, day=17)).kind == "idle"


def test_followup_once_then_wait_for_reply():
    s = make(idle_hours=1, followup_minutes=30)
    s.note_inbound(at(10))
    assert s.tick(at(11, 1)).kind == "idle"
    s.note_outbound(at(11, 2))  # 她主动说了
    assert s.tick(at(11, 31)) is None
    followup = s.tick(at(11, 33))
    assert followup is not None and followup.kind == "followup"
    s.note_outbound(at(11, 34))  # 她追问了
    # 对方一直没回：不再提醒
    for hour in range(12, 24):
        assert s.tick(at(hour)) is None
    assert s.state.paused
    # 对方开口后重新计时
    s.note_inbound(at(15, 0, day=17))
    assert not s.state.paused and s.state.idle_due_at == pytest.approx(at(16, 0, day=17))


def test_followup_is_skipped_if_she_stays_silent_or_quiet_hours_begin():
    s = make(idle_hours=1, followup_minutes=30)
    s.note_inbound(at(10))
    s.tick(at(11, 1))
    s.note_outbound(at(11, 2))
    s.tick(at(11, 33))  # 追问提醒
    s.tick(at(11, 33) + SPEAK_WINDOW + 1)  # 她没追问
    assert s.state.paused

    s2 = make(idle_hours=1, followup_minutes=30)
    s2.note_inbound(at(22, 30))
    s2.tick(at(23, 31))
    s2.note_outbound(at(23, 35))
    assert s2.tick(at(0, 6, day=17)) is None  # 追问时间落在免打扰里：不追问
    assert s2.state.paused


def test_followup_disabled_pauses_right_after_she_speaks():
    s = make(idle_hours=1, followup_minutes=0)
    s.note_inbound(at(10))
    s.tick(at(11, 1))
    s.note_outbound(at(11, 2))
    assert s.state.paused and s.state.followup_due_at is None


def test_normal_reply_restarts_the_idle_timer():
    s = make(idle_hours=2)
    s.note_inbound(at(10))
    s.note_outbound(at(10, 5))
    assert s.state.idle_due_at == pytest.approx(at(12, 5))


def test_reminders_respect_limits_and_fire_on_time():
    s = make(idle_hours=5, schedule_min_minutes=10, schedule_max_days=7, schedule_max_pending=2)
    now = at(10)
    with pytest.raises(ValueError, match="最早"):
        s.add_reminder(now, now + 5 * 60, "太快了")
    with pytest.raises(ValueError, match="最晚"):
        s.add_reminder(now, now + 8 * 86400, "太远了")
    with pytest.raises(ValueError, match="备注"):
        s.add_reminder(now, now + HOUR, "   ")
    first = s.add_reminder(now, at(18), "问问面试结果")
    s.add_reminder(now, at(20), "接着聊电影")
    with pytest.raises(ValueError, match="最多 2 个"):
        s.add_reminder(now, at(21), "第三个")
    assert s.cancel_reminder("nope") is None

    trigger = s.tick(at(18, 0))
    assert trigger is not None and trigger.kind == "reminder" and trigger.reminder.id == first.id
    assert [r.note for r in s.state.reminders] == ["接着聊电影"]


def test_reminder_waits_for_quiet_hours_and_active_chat_but_not_daily_cap():
    s = make(idle_hours=5, daily_max=0)
    s.add_reminder(at(1), at(3), "早上问问睡得好不好")
    assert s.tick(at(3)) is None  # 免打扰
    s.note_inbound(at(9, 0))
    assert s.tick(at(9, 2)) is None  # 正在聊天
    trigger = s.tick(at(9, 6))
    assert trigger is not None and trigger.kind == "reminder"  # 每天次数为 0 也照样提醒


def test_scheduling_pushes_back_the_idle_reminder():
    s = make(idle_hours=2)
    s.note_inbound(at(10))
    s.add_reminder(at(10, 1), at(15), "下午再聊")
    assert s.state.idle_due_at >= at(17) - 1


def test_failed_trigger_is_retried_without_using_up_the_daily_count():
    s = make(idle_hours=1, daily_max=1)
    s.note_inbound(at(10))
    trigger = s.tick(at(11, 1))
    s.trigger_failed(trigger, at(11, 1))
    assert s.state.triggers_today == 0 and s.state.awaiting is None
    assert s.tick(at(11, 12)).kind == "idle"


def test_state_round_trips_and_tolerates_garbage():
    s = make()
    s.note_inbound(at(10))
    s.add_reminder(at(10), at(18), "备注")
    restored = State.from_dict(s.state.to_dict())
    assert restored == s.state
    assert State.from_dict("garbage") == State()
    assert State.from_dict({"reminders": [{"id": 1}], "triggers_today": "x"}).reminders == []


def test_parse_when_and_format_time():
    now = at(10)
    assert parse_when("18:00", now) == at(18)
    assert parse_when("09:00", now) == at(9, 0, day=17)  # 已经过了就是明天
    assert parse_when("2026-10-20 18:30", now) == at(18, 30, day=20)
    assert parse_when("10-18 08:00", now) == at(8, 0, day=18)
    assert parse_when("明天", now) is None
    assert format_time(at(18), now) == "今天 18:00"
    assert format_time(at(9, 30, day=17), now) == "明天 09:30"
    assert format_time(at(9, 0, day=15), now) == "昨天 09:00"
    assert format_time(at(9, 0, day=25), now) == "10月25日 09:00"


def test_intent_texts_stay_short_and_neutral():
    s = make()
    reminder = s.add_reminder(at(10), at(18), "问问面试结果")
    texts = [
        build_intent(Trigger("idle", idle_seconds=5 * HOUR), at(15), can_schedule=True, followup_minutes=30),
        build_intent(Trigger("idle"), at(15), can_schedule=False, followup_minutes=30),
        build_intent(Trigger("reminder", reminder=reminder), at(18), can_schedule=True, followup_minutes=30),
        build_intent(Trigger("followup", idle_seconds=31 * 60), at(18), can_schedule=True, followup_minutes=30),
    ]
    assert "5 个小时" in texts[0] and "proactive_schedule" in texts[0]
    assert "一段时间" in texts[1] and "proactive_schedule" not in texts[1]
    assert "问问面试结果" in texts[2] and "今天" in texts[2]
    assert "31 分钟" in texts[3]
    for text in texts:
        assert len(text) < 200
        assert "不要跟对方提起这条提醒本身" in text
