"""主动聊天插件配置（WebUI 配置页由此自动生成）。"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

# 配置结构有增减时提升版本：MaiBot 只在版本升高时把新字段写回已有的 config.toml
SUPPORTED_CONFIG_VERSION = "1.0.0"
PLUGIN_VERSION = "0.1.0"


class PluginOptions(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用主动聊天。关闭后不再提醒，已约好的提醒保留。",
        json_schema_extra={"label": "启用", "order": 0},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="当前配置结构版本。",
        json_schema_extra={"label": "配置版本", "disabled": True, "hidden": True, "order": 99},
    )


class TargetConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "聊天对象"
    __ui_order__: ClassVar[int] = 1

    platform: str = Field(
        default="imessage",
        description="对方所在的平台，例如 imessage、qq。",
        json_schema_extra={"label": "平台", "placeholder": "imessage", "order": 0},
    )
    user_id: str = Field(
        default="",
        description="对方在这个平台上的账号（iMessage 填手机号，带国家代码）。只对和这个人的私聊生效。",
        json_schema_extra={
            "label": "对方账号",
            "placeholder": "+8180XXXXXXXX",
            "hint": "填好后，对方先发一条消息，插件才能认出这个聊天。",
            "order": 1,
        },
    )


class TimingConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "主动频率"
    __ui_order__: ClassVar[int] = 2

    idle_hours: float = Field(
        default=5.0,
        ge=0.5,
        le=24.0,
        description="双方都没说话多久之后，提醒 AI 可以主动找对方聊聊（小时）。",
        json_schema_extra={"label": "安静多久后可以主动（小时）", "x-widget": "slider", "step": 0.5, "order": 0},
    )
    jitter_percent: float = Field(
        default=30.0,
        ge=0.0,
        le=50.0,
        description="在上面的时间上随机浮动多少，免得每次都卡着同一个时间。30 表示 5 小时会落在 3.5–6.5 小时之间。",
        json_schema_extra={"label": "随机浮动（%）", "x-widget": "slider", "step": 5, "order": 1},
    )
    daily_max: int = Field(
        default=2,
        ge=0,
        le=20,
        description="每天最多因为「安静太久」提醒 AI 几次（她不一定每次都开口）。AI 自己约的提醒和追问不算在内。设为 0 就只保留 AI 自己约的提醒。",
        json_schema_extra={"label": "每天最多主动几次", "x-widget": "slider", "step": 1, "order": 2},
    )
    followup_minutes: float = Field(
        default=30.0,
        ge=0.0,
        le=240.0,
        description="AI 主动说了话、对方多久没回时再提醒她一次（她可以追问，也可以不追问）。追问后对方还没回，就等对方开口。填 0 表示不追问。",
        json_schema_extra={"label": "没回复多久后追问（分钟）", "order": 3},
    )


class QuietConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "免打扰"
    __ui_order__: ClassVar[int] = 3

    enabled: bool = Field(
        default=True,
        description="免打扰时段内不提醒 AI；AI 约在这段时间的提醒，顺延到免打扰结束。",
        json_schema_extra={"label": "启用免打扰", "order": 0},
    )
    start: str = Field(
        default="00:00",
        description="免打扰开始时间（24 小时制），按 MaiBot 所在系统的时间算。",
        json_schema_extra={"label": "开始", "placeholder": "00:00", "pattern": r"^\d{1,2}:\d{2}$", "order": 1},
    )
    end: str = Field(
        default="09:00",
        description="免打扰结束时间（24 小时制）。可以跨午夜，例如 23:00 到 08:00。",
        json_schema_extra={
            "label": "结束",
            "placeholder": "09:00",
            "pattern": r"^\d{1,2}:\d{2}$",
            "hint": "发 /proactive_status 可以看到 MaiBot 系统现在几点，确认时间对不对。",
            "order": 2,
        },
    )


class ScheduleConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "AI 自己约时间"
    __ui_order__: ClassVar[int] = 4

    enabled: bool = Field(
        default=True,
        description="允许 AI 在聊天中约个时间再找对方（例如「明天傍晚问问面试结果」），到点插件提醒她。",
        json_schema_extra={"label": "允许 AI 自己约时间", "order": 0},
    )
    min_minutes: int = Field(
        default=10,
        ge=1,
        le=1440,
        description="AI 最早能约多少分钟以后。",
        json_schema_extra={"label": "最早多久以后（分钟）", "order": 1},
    )
    max_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="AI 最晚能约多少天以内。",
        json_schema_extra={"label": "最晚多少天以内", "x-widget": "slider", "step": 1, "order": 2},
    )
    max_pending: int = Field(
        default=3,
        ge=1,
        le=10,
        description="AI 同时最多能挂着几个约定。",
        json_schema_extra={"label": "同时最多几个约定", "x-widget": "slider", "step": 1, "order": 3},
    )


class ProactiveChatConfig(PluginConfigBase):
    plugin: PluginOptions = Field(default_factory=PluginOptions)
    target: TargetConfig = Field(default_factory=TargetConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    quiet: QuietConfig = Field(default_factory=QuietConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
