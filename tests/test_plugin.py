"""把插件放进一个假的 MaiBot 环境里，走一遍「收消息 → 安静 → 提醒 → 她开口 → 追问」。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import asyncio
import copy
import json

from maibot_sdk.context import PluginContext, PluginPaths

import proactive_chat_under_test as plugin_module

CONFIG = {
    "plugin": {"enabled": True, "config_version": "1.0.0"},
    "target": {"platform": "imessage", "user_id": "+81 80-1234-5678"},
    "timing": {"idle_hours": 1.0, "jitter_percent": 0, "daily_max": 2, "followup_minutes": 30},
    "quiet": {"enabled": True, "start": "00:00", "end": "09:00"},
}


def at(hour: int, minute: int = 0, day: int = 16) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


class FakeHost:
    def __init__(self, trigger_ok: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.trigger_ok = trigger_ok

    async def rpc(self, method: str, plugin_id: str, payload: dict[str, Any], timeout_ms: int | None = None) -> Any:
        self.calls.append((method, payload))
        if method == "cap.call" and payload["capability"] == "maisaka.proactive.trigger":
            return {"success": True, "queued": True} if self.trigger_ok else {"success": False, "error": "没有聊天流"}
        return {"success": True}

    def triggers(self) -> list[str]:
        return [p["args"]["intent"] for name, p in self.calls if name == "cap.call" and p["capability"] == "maisaka.proactive.trigger"]


def make_plugin(tmp_path: Path, host: FakeHost, config: dict | None = None) -> Any:
    plugin = plugin_module.create_plugin()
    plugin._set_context(PluginContext("com.goenzero.proactive_chat", host.rpc, PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "rt")))
    plugin.set_plugin_config(copy.deepcopy(config or CONFIG))
    return plugin


def inbound(user: str = "+818012345678", platform: str = "imessage", group: bool = False, session: str = "s-target") -> dict:
    info: dict[str, Any] = {"user_info": {"user_id": user}}
    if group:
        info["group_info"] = {"group_id": "g1"}
    return {"message": {"platform": platform, "session_id": session, "message_info": info}}


def outbound(session: str = "s-target", sent: bool = True, stored: bool = True) -> dict:
    return {"message": {"platform": "imessage", "session_id": session, "message_info": {}}, "sent": sent, "storage_message": stored}


def run(coro):
    return asyncio.run(coro)


def test_full_cycle_idle_speak_followup_then_wait(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)
    clock = {"now": at(10)}
    monkeypatch.setattr(plugin_module.time, "time", lambda: clock["now"])

    async def main():
        await plugin.on_load()
        plugin._loop_task.cancel()
        # 别人的消息、群消息不算
        await plugin.on_inbound(**inbound(user="+10000000000"))
        await plugin.on_inbound(**inbound(group=True))
        assert plugin._scheduler.state.stream_id == ""
        assert await plugin.check_once(at(10)) is None
        # 对方发消息：认出聊天并开始计时
        await plugin.on_inbound(**inbound())
        assert plugin._scheduler.state.stream_id == "s-target"
        assert await plugin.check_once(at(10, 59)) is None
        trigger = await plugin.check_once(at(11, 1))
        assert trigger is not None and trigger.kind == "idle"
        assert "大约 1 个小时没有说话" in host.triggers()[0]
        # 她开口了
        clock["now"] = at(11, 3)
        await plugin.on_outbound(**outbound())
        assert (await plugin.check_once(at(11, 34))).kind == "followup"
        clock["now"] = at(11, 35)
        await plugin.on_outbound(**outbound())
        assert await plugin.check_once(at(15)) is None
        await plugin.on_unload()

    run(main())
    assert len(host.triggers()) == 2
    trigger_call = next(p for name, p in host.calls if name == "cap.call" and p["capability"] == "maisaka.proactive.trigger")
    assert trigger_call["args"]["stream_id"] == "s-target"
    saved = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert saved["paused"] is True and saved["stream_id"] == "s-target"


def test_failed_trigger_is_retried_later(tmp_path):
    host = FakeHost(trigger_ok=False)
    plugin = make_plugin(tmp_path, host)

    async def main():
        await plugin.on_load()
        plugin._loop_task.cancel()
        plugin._scheduler.state.stream_id = "s-target"
        plugin._scheduler.note_inbound(at(10))
        assert await plugin.check_once(at(11, 1)) is None
        assert plugin._scheduler.state.triggers_today == 0
        host.trigger_ok = True
        assert await plugin.check_once(at(11, 5)) is None  # 10 分钟后才重试
        assert (await plugin.check_once(at(11, 12))).kind == "idle"
        await plugin.on_unload()

    run(main())


def test_schedule_tool_only_in_target_chat_and_reminder_fires(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)
    monkeypatch.setattr(plugin_module.time, "time", lambda: at(10))

    async def main():
        await plugin.on_load()
        plugin._loop_task.cancel()
        await plugin.on_inbound(**inbound())
        # 别的聊天里看不到也用不了
        tools = [{"function": {"name": "proactive_schedule"}}, {"function": {"name": "reply"}}, {"name": "proactive_cancel"}]
        hidden = await plugin.on_planner_request(session_id="s-other", tool_definitions=tools)
        assert [t.get("function", t).get("name") for t in hidden["modified_kwargs"]["tool_definitions"]] == ["reply"]
        assert await plugin.on_planner_request(session_id="s-target", tool_definitions=tools) is None
        refused = await plugin.tool_schedule(note="x", minutes_later=30, stream_id="s-other")
        assert refused["success"] is False

        bad = await plugin.tool_schedule(note="问问面试", minutes_later=3, stream_id="s-target")
        assert bad["success"] is False and "最早" in bad["error"]
        neither = await plugin.tool_schedule(note="问问面试", stream_id="s-target")
        assert neither["success"] is False
        ok = await plugin.tool_schedule(note="问问面试结果", at="18:00", stream_id="s-target")
        assert ok["success"] is True and "今天 18:00" in ok["content"]
        early = await plugin.tool_schedule(note="早上问好", at="07:30", stream_id="s-target")
        assert "免打扰" in early["content"]
        reminder_id = plugin._scheduler.state.reminders[0].id  # 按时间排序：今天 18:00 在前
        cancelled = await plugin.tool_cancel(reminder_id=reminder_id, stream_id="s-target")
        assert cancelled["success"] is True and "问问面试结果" in cancelled["content"]

        trigger = await plugin.check_once(at(7, 31, day=17))
        assert trigger is None  # 免打扰
        trigger = await plugin.check_once(at(9, 0, day=17))
        assert trigger is not None and trigger.kind == "reminder"
        await plugin.on_unload()

    run(main())
    assert "早上问好" in host.triggers()[-1]


def test_status_and_config_change(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)
    monkeypatch.setattr(plugin_module.time, "time", lambda: at(10))

    async def main():
        await plugin.on_load()
        plugin._loop_task.cancel()
        assert "等 imessage:+818012345678 先发一条消息" in plugin.status_text(at(10), show_notes=False)
        await plugin.on_inbound(**inbound())
        await plugin.tool_schedule(note="秘密备注", minutes_later=60, stream_id="s-target")
        text = plugin.status_text(at(10), show_notes=False)
        assert "系统时间：2026-10-16 10:00" in text and "下次「安静太久」提醒" in text and "秘密备注" not in text
        assert "秘密备注" in plugin.status_text(at(10), show_notes=True)

        await plugin.handle_status(stream_id="s-target")
        sent = [p for name, p in host.calls if name == "cap.call" and p["capability"] == "send.text"]
        assert "秘密备注" in sent[-1]["args"]["text"]
        assert sent[-1]["args"]["storage_message"] is False  # 状态不进聊天记录和记忆

        # 改频率：立即生效，不丢状态
        data = copy.deepcopy(CONFIG)
        data["timing"]["idle_hours"] = 3
        plugin.set_plugin_config(data)
        await plugin.on_config_update("self", data, "")
        assert plugin._scheduler.settings.idle_hours == 3 and plugin._scheduler.state.stream_id == "s-target"
        # 换聊天对象：重新开始
        data["target"]["user_id"] = "+819099999999"
        plugin.set_plugin_config(data)
        await plugin.on_config_update("self", data, "")
        assert plugin._scheduler.state.stream_id == "" and plugin._scheduler.state.reminders == []
        await plugin.on_unload()

    run(main())


def test_state_survives_restart(tmp_path):
    host = FakeHost()

    async def first():
        plugin = make_plugin(tmp_path, host)
        await plugin.on_load()
        plugin._loop_task.cancel()
        await plugin.on_inbound(**inbound())
        plugin._scheduler.add_reminder(at(10), at(18), "重启也别忘")
        await plugin.on_unload()

    async def second():
        plugin = make_plugin(tmp_path, host)
        await plugin.on_load()
        plugin._loop_task.cancel()
        assert plugin._scheduler.state.stream_id == "s-target"
        assert [r.note for r in plugin._scheduler.state.reminders] == ["重启也别忘"]
        await plugin.on_unload()

    run(first())
    run(second())


def test_disabled_or_unsent_does_nothing(tmp_path):
    host = FakeHost()
    data = copy.deepcopy(CONFIG)
    data["plugin"]["enabled"] = False
    plugin = make_plugin(tmp_path, host, data)

    async def main():
        await plugin.on_load()
        plugin._loop_task.cancel()
        await plugin.on_inbound(**inbound())
        plugin._scheduler.state.idle_due_at = at(9)
        assert await plugin.check_once(at(12)) is None
        before = plugin._scheduler.state.last_outbound_at
        await plugin.on_outbound(**outbound(sent=False))
        await plugin.on_outbound(**outbound(stored=False))  # 命令回复不算 AI 说话
        assert plugin._scheduler.state.last_outbound_at == before
        await plugin.on_unload()

    run(main())
    assert host.triggers() == []
