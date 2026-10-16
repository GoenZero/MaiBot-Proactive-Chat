"""交给 MaiBot 主动任务的提醒文字。

MaiBot 会把它包成一条系统提醒放进 AI 当前的上下文（不写进聊天记录数据库，表达学习也不会学），
并在后面加上「请结合当前聊天关系、记忆和上下文，自行决定是否回复以及如何表达」。
所以这里只写事实和可选项，写短、写中性，不替 AI 编任何事。
"""

from __future__ import annotations

from .scheduler import Trigger, describe_duration, format_time

SCHEDULE_TOOL = "proactive_schedule"
_DONT_MENTION = "不要跟对方提起这条提醒本身。"


def build_intent(trigger: Trigger, now: float, *, can_schedule: bool, followup_minutes: float) -> str:
    if trigger.kind == "reminder" and trigger.reminder is not None:
        reminder = trigger.reminder
        return (
            f"到了你之前约好的时间（{format_time(reminder.created_at, now)}约的）。你当时记下的是：「{reminder.note}」。"
            f"可以照这个主动找对方，也可以看情况不说。{_DONT_MENTION}"
        )
    if trigger.kind == "followup":
        waited = describe_duration(trigger.idle_seconds or followup_minutes * 60)
        return f"你刚才主动发的消息，对方 {waited}还没回。可以轻轻追问一句，也可以先不打扰。{_DONT_MENTION}"
    quiet_for = f"大约 {describe_duration(trigger.idle_seconds)}" if trigger.idle_seconds > 0 else "有一段时间"
    text = (
        f"对方{quiet_for}没有说话了。"
        "如果想起之前没聊完的事、对方提过的打算，或者有想分享的事，可以主动找对方聊聊；觉得现在不合适就不说。"
    )
    if can_schedule:
        text += f"想晚一点再找对方，可以用 {SCHEDULE_TOOL} 约个时间。"
    return text + _DONT_MENTION
