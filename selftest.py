"""librunner 离线自测 / 回归检查。

覆盖：runner 类型别名、补丁应用与撤销、注册表与配置放行、WebUI 元数据注入、
残留类型降级策略，以及走真实 ``ThirdPartyAgentSubStage.process`` 的分发链路。

用法（在 AstrBot 仓库中，使用该实例的 uv 环境）::

    uv run python data/plugins/astrbot_plugin_librunner/selftest.py

脚本会把 ``ASTRBOT_ROOT`` 指向临时目录（除非调用方已显式指定），因此不会读写
真实实例的 ``data/plugin_data``。全部检查通过时退出码为 0。
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("ASTRBOT_DISABLE_METRICS", "1")
if "ASTRBOT_ROOT" not in os.environ:
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="librunner-selftest-")

# 「前置插件」检查要求插件目录里存在前置：临时 root 下补一个占位目录，避免离线自测
# 触发真实的 gh/git 安装（安装流程由 §9 用本地仓库单独演练）。
_PREREQUISITE_NAME = "astrbot_plugin_libi18nject"
_PREREQUISITE_DIR = (
    Path(os.environ["ASTRBOT_ROOT"]) / "data" / "plugins" / _PREREQUISITE_NAME
)
if not (_PREREQUISITE_DIR / "metadata.yaml").is_file():
    _PREREQUISITE_DIR.mkdir(parents=True, exist_ok=True)
    (_PREREQUISITE_DIR / "metadata.yaml").write_text(
        f"name: {_PREREQUISITE_NAME}\n", encoding="utf-8"
    )

import asyncio
import shutil
from types import ModuleType, SimpleNamespace

import astrbot.core.config.agent_runner as ar
import astrbot.core.config.default as cfg_default
import astrbot.core.pipeline.process_stage.method.agent_sub_stages.third_party as tp
import astrbot.core.utils.migra_helper as migra_helper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain, ResultContentType
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.context import Context

from data.plugins.astrbot_plugin_librunner import api
from data.plugins.astrbot_plugin_librunner import patcher as patcher_mod
from data.plugins.astrbot_plugin_librunner import prerequisite as prerequisite_mod
from data.plugins.astrbot_plugin_librunner.echo import (
    DEFAULT_ECHO_REPLY,
    ECHO_RUNNER_TYPE,
    EchoAgentRunner,
)
from data.plugins.astrbot_plugin_librunner.main import LibrunnerPlugin
from data.plugins.astrbot_plugin_librunner.patcher import CorePatcher, _RunnerTypeAlias

try:  # 可选姊妹插件：WebUI 文案注入由它负责，未安装时跳过相关检查
    from data.plugins.astrbot_plugin_libi18nject import registry as libi18n_registry
except Exception:  # noqa: BLE001 - 缺少姊妹插件不影响其余检查
    libi18n_registry = None

BUILTIN = ("local", "dify", "coze", "dashscope", "deerflow")
FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  <- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def schema_item():
    return cfg_default.CONFIG_METADATA_3["ai_group"]["metadata"]["agent_runner"]["items"][
        "agent_runner.runner_type"
    ]


def schema_groups():
    return cfg_default.CONFIG_METADATA_3["ai_group"]["metadata"]


def schema_group(runner_type):
    return schema_groups().get(f"{runner_type}_runner")


#: 内置执行器配置分组的原始内容：用于验证补丁不污染内核自带分组。
BUILTIN_GROUPS_BEFORE = copy.deepcopy(
    {key: schema_group(key) for key in ("dify", "coze", "dashscope", "deerflow")}
)


# ------------------------------------------------------------- 0. 别名行为
alias = _RunnerTypeAlias("dummy", "deerflow")
check("别名等于真实类型", alias == "dummy")
check("别名等于占位分支", alias == "deerflow")
check("别名不等于其它内置类型", not (alias == "dify"))
check("别名字符串值真实", str(alias) == "dummy" and f"{alias}" == "dummy")
check("别名可哈希", isinstance(hash(alias), int))
check("别名 in 元组判断", alias in ("dify", "deerflow"))

# ------------------------------------------------------------- 1. 插件生命周期
plugin = LibrunnerPlugin(context=None, config={"orphan_runner_policy": "strict"})
asyncio.run(plugin.initialize())
check("插件 initialize 应用补丁", api.get_status()["patched"] is True)
check("插件读取合法配置策略", plugin._patcher.orphan_policy == "strict")

bad = LibrunnerPlugin(context=None, config={"orphan_runner_policy": "nonsense"})
asyncio.run(bad.initialize())
check("非法策略回退默认值", bad._patcher.orphan_policy == "fallback_local")
asyncio.run(bad.terminate())
asyncio.run(plugin.terminate())
check("插件 terminate 撤销补丁", api.get_status()["patched"] is False)

# ------------------------------------------------------------- 2. 注册
built = []


class DummyRunner:
    def __init__(self):
        self.reset_kwargs = None
        self.closed = False
        self.stepped = False

    async def reset(self, **kwargs):
        self.reset_kwargs = kwargs

    async def step_until_done(self, max_step=128):
        self.stepped = True
        self.max_step = max_step
        self.dispatch_active = patcher_mod._active_dispatch.get() is not None
        self.slot_is_factory = not hasattr(tp.DeerFlowAgentRunner[AstrAgentContext], "__origin__")
        yield SimpleNamespace(type="streaming_delta", data={"chain": MessageChain().message("chunk-A")})

    def done(self):
        return True

    def get_final_llm_resp(self):
        return None

    async def close(self):
        self.closed = True


def factory():
    runner = DummyRunner()
    built.append(runner)
    return runner


patcher = CorePatcher()
check("CorePatcher.apply()", patcher.apply(), api.get_status().get("patch_error") or "")
check("未注册时类型未放行", "dummy" not in ar.AGENT_RUNNER_TYPES)

info = api.register_runner(
    "dummy",
    factory,
    config_default={"a": 1},
    config_metadata={"a": {"description": "测试字段", "hint": "hint 文本"}},
    label="Dummy",
    owner="selftest",
    reset_kwargs={"settings": {"x": 1}},
)
check("register_runner 返回快照", info.runner_type == "dummy" and info.owner == "selftest")
check(
    "register_runner 带出 config_metadata",
    info.config_metadata == {"a": {"description": "测试字段", "hint": "hint 文本"}},
    str(info.config_metadata),
)
check("注册后放行类型", "dummy" in ar.AGENT_RUNNER_TYPES)
check("注册后注入配置默认值", ar.AGENT_RUNNER_CONFIG_DEFAULTS.get("dummy") == {"a": 1})
check("内置类型顺序未被破坏", ar.AGENT_RUNNER_TYPES[:5] == BUILTIN)
check("THIRD_PARTY_AGENT_RUNNER_TYPES 未被改动", ar.THIRD_PARTY_AGENT_RUNNER_TYPES == BUILTIN[1:])
same = api.register_runner(
    "dummy",
    factory,
    config_default={"a": 1},
    config_metadata={"a": {"description": "测试字段", "hint": "hint 文本"}},
    label="Dummy",
    owner="selftest",
    reset_kwargs={"settings": {"x": 1}},
)
check(
    "重复注册幂等（覆盖同 owner 的注册项）",
    same.runner_type == "dummy" and len(api.list_runners()) == 1,
)
try:
    api.register_runner("dummy", factory, owner="other")
    check("他人抢占被拒绝", False)
except ValueError:
    check("他人抢占被拒绝", True)
try:
    api.register_runner("dify", factory, owner="selftest")
    check("内置类型不可注入", False)
except ValueError:
    check("内置类型不可注入", True)
try:
    api.register_runner("bad type!", factory, owner="selftest")
    check("非法 runner_type 被拒绝", False)
except ValueError:
    check("非法 runner_type 被拒绝", True)
try:
    api.register_runner("meta_probe", factory, config_metadata={"x": 5}, owner="selftest")
    check("config_metadata 值类型非法被拒绝", False)
except TypeError:
    check("config_metadata 值类型非法被拒绝", True)
try:
    api.register_runner("meta_probe", factory, config_metadata={1: "x"}, owner="selftest")
    check("config_metadata 字段名非法被拒绝", False)
except ValueError:
    check("config_metadata 字段名非法被拒绝", True)

api.register_runner(
    "meta_probe",
    factory,
    config_default={"n": 1, "s": "t", "flag": True, "ratio": 1.5, "obj": {}, "arr": []},
    config_metadata={"s": "说明文本"},
    owner="selftest",
)
probe_fields = ("n", "s", "flag", "ratio", "obj", "arr")
probe_items = (schema_group("meta_probe") or {}).get("items", {})
probe_keys = [f"agent_runner.config.{field}" for field in probe_fields]
probe_types = [probe_items.get(key, {}).get("type") for key in probe_keys]
check(
    "分组字段顺序与 config_default 一致",
    list(probe_items) == probe_keys,
    str(list(probe_items)),
)
check(
    "字符串形式的 config_metadata 被当作描述",
    probe_items.get("agent_runner.config.s", {}).get("description") == "说明文本",
    str(probe_items.get("agent_runner.config.s")),
)
check(
    "未声明的字段以字段名为描述",
    probe_items.get("agent_runner.config.n", {}).get("description") == "n",
    str(probe_items.get("agent_runner.config.n")),
)
check(
    "字段类型按默认值推断",
    probe_types == ["int", "string", "bool", "float", "dict", "list"],
    str(probe_types),
)
api.unregister_runner("meta_probe", owner="selftest")
check("注销后配置分组被回收", schema_group("meta_probe") is None)
check("注销后类型不再放行", "meta_probe" not in ar.AGENT_RUNNER_TYPES)

normalized = ar.normalize_agent_runner({"runner_type": "dummy", "config": {"a": 2, "unknown": 3}})
check("normalize 接受注入类型", normalized["runner_type"] == "dummy")
check("config_default 充当字段白名单", normalized["config"] == {"a": 2}, str(normalized))

late = ModuleType("librunner_late_import_probe")
exec("from astrbot.core.config.agent_runner import normalize_agent_runner", late.__dict__)
check("补丁后新导入的模块拿到新 normalize", late.normalize_agent_runner is ar.normalize_agent_runner)
check("补丁前已导入的模块被重绑", migra_helper.normalize_agent_runner is ar.normalize_agent_runner)

item = schema_item()
check("WebUI options 含注入类型", "dummy" in item["options"])
check("WebUI runner_defaults 含注入类型", item.get("runner_defaults", {}).get("dummy") == {"a": 1})
check("WebUI labels 与 options 对齐", len(item["labels"]) == len(item["options"]), f"{len(item['labels'])} vs {len(item['options'])}")
check("WebUI options 保留内置项", item["options"][:5] == list(BUILTIN))

group = schema_group("dummy")
check("WebUI 注入执行器配置分组", isinstance(group, dict), str(group))
check(
    "分组只在该执行器被选中时显示",
    (group or {}).get("condition")
    == {"provider_settings.enable": True, "agent_runner.runner_type": "dummy"},
    str((group or {}).get("condition")),
)
check(
    "分组标题取注册项 label",
    (group or {}).get("description") == "Dummy",
    str((group or {}).get("description")),
)
group_items = (group or {}).get("items", {})
check(
    "分组字段映射到 agent_runner.config 并推断类型",
    group_items.get("agent_runner.config.a", {}).get("type") == "int",
    str(group_items),
)
check(
    "config_metadata 的文案被写进分组",
    group_items.get("agent_runner.config.a", {}).get("hint") == "hint 文本",
    str(group_items),
)
check(
    "补丁未污染内核自带分组",
    {key: schema_group(key) for key in BUILTIN_GROUPS_BEFORE} == BUILTIN_GROUPS_BEFORE,
    str({key: schema_group(key) for key in BUILTIN_GROUPS_BEFORE}),
)

# WebUI 文案（执行器下拉框、「更换执行方式」对话框、配置分组标题）由可选的姊妹
# 插件 astrbot_plugin_libi18nject 负责注入；librunner 只负责构造并登记这些文案，
# 见 main._refresh_i18n。
i18n_probe = LibrunnerPlugin(context=None, config={})
probe_entries = i18n_probe._i18n_entries()
probe_table = probe_entries.get("*", {})
probe_prefix = "features.config.aiSettings.runners.dummy."
check(
    "为已注册执行器构造 WebUI 词条",
    probe_table.get(f"{probe_prefix}title") == "Dummy"
    and probe_table.get(f"{probe_prefix}summary") == "当前由 Dummy 处理 AI 对话。"
    and probe_table.get(f"{probe_prefix}description")
    == probe_table.get(f"{probe_prefix}summary"),
    str(probe_table),
)
check(
    "词条登记到通配 locale 作兜底",
    list(probe_entries) == ["*"],
    str(list(probe_entries)),
)
probe_sections = i18n_probe._plain_text_sections()
check(
    "为执行器配置分组构造纯文本版本",
    list(probe_sections) == ["dummy_runner"]
    and probe_sections["dummy_runner"].get("description") == "Dummy"
    and probe_sections["dummy_runner"]
    .get("items", {})
    .get("agent_runner.config.a", {})
    .get("hint")
    == "hint 文本",
    str(probe_sections),
)

slot_proxy = tp.DeerFlowAgentRunner
check("占位分支已被代理", getattr(slot_proxy, "_librunner_slot_proxy", False) is True)
check("代理透传真实类", tp.DeerFlowAgentRunner[AstrAgentContext].__origin__ is slot_proxy.__dict__["_real"])
check("占位类型常量未被改动", tp.DEERFLOW_PROVIDER_TYPE == "deerflow")

# ------------------------------------------------------------- 3. 真实分发链路
class TestEvent(AstrMessageEvent):
    """真实 AstrMessageEvent 的最小子类（无抽象方法需要实现）。"""


def make_event(text="hello"):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "bot-1"
    message.session_id = "session-1"
    message.message_id = "msg-1"
    message.group = None
    message.sender = MessageMember(user_id="user-1")
    message.message = []
    message.message_str = text
    message.raw_message = None
    return TestEvent(
        text,
        message,
        PlatformMetadata(name="testplat", description="离线测试平台", id="testplat-1"),
        "session-1",
    )


def make_context():
    star_context = Context(
        event_queue=None,
        config=None,
        db=None,
        provider_manager=None,
        platform_manager=None,
        conversation_manager=None,
        message_history_manager=None,
        persona_manager=None,
        astrbot_config_mgr=None,
        knowledge_base_manager=None,
        cron_manager=None,
    )
    return SimpleNamespace(plugin_manager=SimpleNamespace(context=star_context))


def make_stage(runner_type, runner_config=None):
    stage = tp.ThirdPartyAgentSubStage()
    stage.ctx = make_context()
    stage.runner_config = {} if runner_config is None else runner_config
    stage.runner_type = runner_type
    stage.streaming_response = False
    stage.unsupported_streaming_strategy = "turn_off"
    stage.stream_consumption_close_timeout_sec = 30
    return stage


async def drive(stage, event):
    async for _ in tp.ThirdPartyAgentSubStage.process(stage, event, ""):
        pass


stage = make_stage("dummy", {"a": 2})
event = make_event()
asyncio.run(drive(stage, event))

check("factory 被调用一次", len(built) == 1)
runner = built[0] if built else None
reset_kwargs = runner.reset_kwargs if runner else None
check("runner.reset 被调用", reset_kwargs is not None)
if reset_kwargs:
    check("reset 收到内核参数", reset_kwargs.get("request") is not None and reset_kwargs.get("agent_hooks") is not None)
    check("reset 收到 run_context", reset_kwargs.get("run_context") is not None)
    check("reset 收到 streaming=False", reset_kwargs.get("streaming") is False)
    check("reset 收到 provider_config", reset_kwargs.get("provider_config") == {"a": 2}, str(reset_kwargs.get("provider_config")))
    check("reset 收到注入的额外参数", reset_kwargs.get("settings") == {"x": 1}, str(reset_kwargs.get("settings")))
    check("request.prompt 正确", reset_kwargs["request"].prompt == "hello", reset_kwargs["request"].prompt)
    check("request.session_id 正确", reset_kwargs["request"].session_id == event.unified_msg_origin, reset_kwargs["request"].session_id)
    check("run_context 包装了真实 agent 上下文", isinstance(reset_kwargs["run_context"].context, AstrAgentContext))
check("step_until_done 被调用", bool(runner and runner.stepped))
check("runner.close 被调用", bool(runner and runner.closed))
chain_texts = [comp.text for comp in (event.get_result().chain if event.get_result() else [])]
check("结果写回事件", chain_texts == ["chunk-A"], str(chain_texts))
check("原实例 runner_type 未被污染", stage.runner_type == "dummy")

stage2 = make_stage("nonexistent")
try:
    asyncio.run(drive(stage2, make_event()))
    check("未注册类型透传内核 ValueError", False, "没有抛错")
except ValueError as exc:
    check("未注册类型透传内核 ValueError", "Unsupported third party agent runner type" in str(exc), str(exc))

check("内置类型不在注册表中", all(api._get_spec(t) is None for t in BUILTIN))
check("分发期间 ContextVar 生效", bool(runner and runner.dispatch_active))
check("分发期间占位分支返回注入 factory", bool(runner and runner.slot_is_factory))
check("分发结束后 ContextVar 复位", patcher_mod._active_dispatch.get() is None)
check("step_until_done 使用内核的 max_step=128", bool(runner and runner.max_step == 128), str(getattr(runner, "max_step", None)))

# ------------------------------------------------------------- 4. 残留类型降级
api.unregister_runner("dummy", owner="selftest")
check("注销后类型被移除", "dummy" not in ar.AGENT_RUNNER_TYPES)
check("注销后配置默认值被移除", "dummy" not in ar.AGENT_RUNNER_CONFIG_DEFAULTS)
check("注销后 WebUI options 被移除", "dummy" not in schema_item()["options"])
check("注销后仍记录历史注入", "dummy" in api.known_injected_types())
check("注销他人注册项被拒绝", api.unregister_runner("other", owner="nobody") is False)
check("注销后配置分组被回收", schema_group("dummy") is None)

normalized = ar.normalize_agent_runner({"runner_type": "dummy", "config": {}})
check("残留类型回退内置 Agent", normalized["runner_type"] == "local", str(normalized))
try:
    ar.normalize_agent_runner({"runner_type": "typo_type", "config": {}})
    check("未知类型仍抛 ValueError", False)
except ValueError:
    check("未知类型仍抛 ValueError", True)

patcher.revert()
check("revert 后放行列表还原", ar.AGENT_RUNNER_TYPES == BUILTIN, str(ar.AGENT_RUNNER_TYPES))
check("revert 后配置默认值还原", "dummy" not in ar.AGENT_RUNNER_CONFIG_DEFAULTS)
check("revert 后 WebUI options 还原", schema_item()["options"] == list(BUILTIN))
check("revert 后 labels 还原", len(schema_item()["labels"]) == len(BUILTIN))
check(
    "revert 后内核自带分组原样还原",
    {key: schema_group(key) for key in BUILTIN_GROUPS_BEFORE} == BUILTIN_GROUPS_BEFORE,
    str({key: schema_group(key) for key in BUILTIN_GROUPS_BEFORE}),
)
check("revert 后注入分组已被移除", schema_group("dummy") is None)
check("revert 后占位分支还原", tp.DeerFlowAgentRunner is slot_proxy.__dict__["_real"])
check("revert 后 process 还原", not getattr(tp.ThirdPartyAgentSubStage, "_librunner_process_patched", False))
check("revert 后 normalize 还原", not getattr(ar.normalize_agent_runner, "_librunner_wrapped", False))
check("revert 后状态复位", api.get_status()["patched"] is False)
try:
    ar.normalize_agent_runner({"runner_type": "dummy", "config": {}})
    check("revert 后残留类型不再降级", False)
except ValueError:
    check("revert 后残留类型不再降级", True)

# ------------------------------------------------------------- 5. strict 策略
strict_patcher = CorePatcher(orphan_policy="strict")
strict_patcher.apply()
try:
    ar.normalize_agent_runner({"runner_type": "dummy", "config": {}})
    check("strict 策略保持原生报错", False)
except ValueError:
    check("strict 策略保持原生报错", True)
strict_patcher.revert()

# ------------------------------------------------------------- 6. 重复 apply / 残留补丁回收
first = CorePatcher()
first.apply()
second = CorePatcher()
second.apply()
check("重复 apply 不叠加补丁链", tp.ThirdPartyAgentSubStage.process.__name__ == "_patched_process")
second.revert()
check("第二个实例 revert 后已还原", not getattr(tp.ThirdPartyAgentSubStage, "_librunner_process_patched", False))
first.revert()

# ------------------------------------------------------------- 7. /librunner 文本
plugin2 = LibrunnerPlugin(context=None, config={})
asyncio.run(plugin2.initialize())
api.register_runner("dummy", factory, config_default={"a": 1, "b": 2}, owner="selftest")
if libi18n_registry is None:
    print(
        "[SKIP] 前置插件不在场：跳过文案登记检查"
        "（librunner 运行时会自动安装前置，见 §9）"
    )
else:
    check("已安装 libi18nject（WebUI 文案注入）", True)
    live_entries = libi18n_registry.merged_entries().get("*", {})
    live_sections = libi18n_registry.merged_plain_sections()
    check(
        "注册变化后文案同步给 libi18nject",
        live_entries.get("features.config.aiSettings.runners.dummy.title") == "dummy"
        and live_entries.get("features.config.aiSettings.runners.echo.title") == "Echo",
        str(sorted(live_entries)),
    )
    check(
        "配置分组同步给 libi18nject",
        live_sections.get("dummy_runner", {}).get("description") == "dummy"
        and isinstance(live_sections.get("echo_runner"), dict),
        str(sorted(live_sections)),
    )
text = plugin2._render_status()
check("状态文本含补丁状态", "内核补丁: 已应用" in text, text)
check("状态文本含注册项", "dummy" in text and "owner=selftest" in text, text)
check("状态文本含配置字段数", "配置字段 2 项" in text, text)
api.unregister_runner("dummy", owner="selftest")
asyncio.run(plugin2.terminate())

# ------------------------------------------------------------- 8. 自带 echo 执行器
ECHO_TEXT = "自定义 echo 文本 ✅"

echo_plugin = LibrunnerPlugin(context=None, config={"echo_reply": ECHO_TEXT})
asyncio.run(echo_plugin.initialize())
check("启动时自动注册 echo 执行器", api.has_runner(ECHO_RUNNER_TYPE))
echo_info = api.get_runner(ECHO_RUNNER_TYPE)
check(
    "echo 的 owner 是本插件",
    bool(echo_info and echo_info.owner == api.PLUGIN_NAME),
    str(echo_info),
)
check("echo 类型已放行给内核", ECHO_RUNNER_TYPE in ar.AGENT_RUNNER_TYPES)
check("WebUI options 含 echo", ECHO_RUNNER_TYPE in schema_item()["options"])
check(
    "echo 的默认配置进入 runner_defaults",
    schema_item()["runner_defaults"].get(ECHO_RUNNER_TYPE) == {"echo_reply": ECHO_TEXT},
    str(schema_item()["runner_defaults"].get(ECHO_RUNNER_TYPE)),
)
check(
    "echo 在 agent_runner.config 中放行 echo_reply",
    ar.normalize_agent_runner(
        {"runner_type": ECHO_RUNNER_TYPE, "config": {"echo_reply": "面板文本", "noise": 1}}
    )["config"]
    == {"echo_reply": "面板文本"},
    str(ar.normalize_agent_runner({"runner_type": ECHO_RUNNER_TYPE, "config": {}})),
)
echo_group = schema_group(ECHO_RUNNER_TYPE)
echo_items = (echo_group or {}).get("items", {})
check("echo 有 WebUI 配置分组", isinstance(echo_group, dict), str(echo_group))
check(
    "echo 分组含「回复内容」字段",
    echo_items.get("agent_runner.config.echo_reply", {}).get("type") == "string"
    and echo_items.get("agent_runner.config.echo_reply", {}).get("description") == "回复内容",
    str(echo_items),
)


async def echo_runner_probe():
    probe_runner = EchoAgentRunner()
    await probe_runner.reset(
        request=None,
        run_context=None,
        agent_hooks=None,
        provider_config={},
        streaming=True,
        echo_reply=ECHO_TEXT,
    )
    types = [response.type async for response in probe_runner.step_until_done(max_step=8)]
    return types, probe_runner.done(), probe_runner.get_final_llm_resp()


echo_types, echo_done, echo_final = asyncio.run(echo_runner_probe())
check("echo 产出 streaming_delta 与 llm_result", echo_types == ["streaming_delta", "llm_result"], str(echo_types))
check("echo 一步后即完成", echo_done)
check(
    "echo 最终结果符合内核契约",
    echo_final is not None
    and echo_final.role == "assistant"
    and echo_final.result_chain is not None
    and echo_final.result_chain.get_plain_text() == ECHO_TEXT,
    str(echo_final),
)

# 非流式分发（走 streaming_delta）
echo_event = make_event("你好")
asyncio.run(drive(make_stage(ECHO_RUNNER_TYPE), echo_event))
echo_result = echo_event.get_result()
echo_chain = [comp.text for comp in (echo_result.chain if echo_result else [])]
check("非流式分发回复配置文本", echo_chain == [ECHO_TEXT], str(echo_chain))
check(
    "非流式分发未标记错误",
    echo_event.get_extra("_third_party_runner_error") is False,
    str(echo_event.get_extra("_third_party_runner_error")),
)

# 非流式分发（平台不支持流式：内核走 llm_result 分支）
plain_event = make_event("你好")
plain_event.platform_meta.support_streaming_message = False
asyncio.run(drive(make_stage(ECHO_RUNNER_TYPE), plain_event))
plain_result = plain_event.get_result()
plain_chain = [comp.text for comp in (plain_result.chain if plain_result else [])]
check("不支持流式的平台走 llm_result 分支", plain_chain == [ECHO_TEXT], str(plain_chain))
check(
    "llm_result 分支结果类型正确",
    plain_result is not None and plain_result.result_content_type == ResultContentType.LLM_RESULT,
    str(plain_result.result_content_type if plain_result else None),
)


async def drive_streaming(stage, stream_event):
    """驱动一次流式分发，并按真实管线的顺序消费异步流。"""
    chunks = []
    async for _ in tp.ThirdPartyAgentSubStage.process(stage, stream_event, ""):
        result = stream_event.get_result()
        stream = result.async_stream if result is not None else None
        if stream is not None:
            async for chain in stream:
                chunks.append(chain.get_plain_text())
    return chunks


stream_stage = make_stage(ECHO_RUNNER_TYPE)
stream_stage.streaming_response = True
stream_stage.unsupported_streaming_strategy = "turn_on"
stream_event = make_event("你好")
stream_chunks = asyncio.run(drive_streaming(stream_stage, stream_event))
check("流式分发推送增量", stream_chunks == [ECHO_TEXT], str(stream_chunks))
stream_result = stream_event.get_result()
check(
    "流式结束结果是 STREAMING_FINISH",
    stream_result is not None
    and stream_result.result_content_type == ResultContentType.STREAMING_FINISH,
    str(stream_result.result_content_type if stream_result else None),
)
stream_chain = [comp.text for comp in (stream_result.chain if stream_result else [])]
check("流式结束结果内容正确", stream_chain == [ECHO_TEXT], str(stream_chain))

# 面板上的执行器配置（agent_runner.config）优先于插件配置项
PANEL_TEXT = "面板上的回复文本 🎛️"
panel_event = make_event("你好")
asyncio.run(drive(make_stage(ECHO_RUNNER_TYPE, {"echo_reply": PANEL_TEXT}), panel_event))
panel_result = panel_event.get_result()
panel_chain = [comp.text for comp in (panel_result.chain if panel_result else [])]
check("面板配置优先于插件配置项", panel_chain == [PANEL_TEXT], str(panel_chain))

# 面板值留空时回落到插件配置项
blank_panel_event = make_event("你好")
asyncio.run(drive(make_stage(ECHO_RUNNER_TYPE, {"echo_reply": "  "}), blank_panel_event))
blank_panel_result = blank_panel_event.get_result()
blank_panel_chain = [comp.text for comp in (blank_panel_result.chain if blank_panel_result else [])]
check("面板留空时回落到插件配置项", blank_panel_chain == [ECHO_TEXT], str(blank_panel_chain))

# 配置改成空白后应回落到内置兜底文案（同时验证每次都现取配置）
echo_plugin.plugin_config = {"echo_reply": "   "}
blank_event = make_event("你好")
asyncio.run(drive(make_stage(ECHO_RUNNER_TYPE), blank_event))
blank_result = blank_event.get_result()
blank_chain = [comp.text for comp in (blank_result.chain if blank_result else [])]
check("echo_reply 留空时使用兜底文案", blank_chain == [DEFAULT_ECHO_REPLY], str(blank_chain))

asyncio.run(echo_plugin.terminate())
check("terminate 注销 echo 执行器", not api.has_runner(ECHO_RUNNER_TYPE))
check("terminate 后 echo 类型被收回", ECHO_RUNNER_TYPE not in ar.AGENT_RUNNER_TYPES)
check("terminate 后 WebUI options 收回 echo", ECHO_RUNNER_TYPE not in schema_item()["options"])
check("terminate 后 echo 配置分组被收回", schema_group(ECHO_RUNNER_TYPE) is None)
check(
    "terminate 后内核自带分组仍原样",
    {key: schema_group(key) for key in BUILTIN_GROUPS_BEFORE} == BUILTIN_GROUPS_BEFORE,
    str({key: schema_group(key) for key in BUILTIN_GROUPS_BEFORE}),
)

# ------------------------------------------------------------- 9. 前置插件管理
prereq = prerequisite_mod.Prerequisite(
    prerequisite_mod.DEFAULT_PREREQUISITE_NAME,
    prerequisite_mod.DEFAULT_PREREQUISITE_REPO,
)
check(
    "默认前置是 libi18nject",
    prereq.name == _PREREQUISITE_NAME
    and prereq.repo == "cxykevin/astrbot_plugin_libi18nject",
    prereq.describe(),
)

# 9.1 检测规则
empty_store = Path(tempfile.mkdtemp(prefix="librunner-prereq-")) / "plugins"
check(
    "目录不存在时判定为未安装",
    prerequisite_mod.is_installed(prereq, store=empty_store) is False,
)
stub_dir = empty_store / prereq.name
stub_dir.mkdir(parents=True)
check(
    "只有空目录仍算未安装",
    prerequisite_mod.is_installed(prereq, store=empty_store) is False,
)
(stub_dir / "metadata.yaml").write_text(
    f"name: {prereq.name}\n", encoding="utf-8"
)
check(
    "含 metadata.yaml 即算已安装",
    prerequisite_mod.is_installed(prereq, store=empty_store) is True,
)
check(
    "解析出安装目录",
    prerequisite_mod.installed_path(prereq, store=empty_store) == stub_dir,
)

# 9.2 克隆命令构造
clone_target = empty_store / "target"
git_command = prerequisite_mod.clone_commands(prereq, clone_target, use_gh=False)
check(
    "git 回退命令使用完整 GitHub 地址",
    bool(git_command)
    and git_command[0][1][-1] == str(clone_target)
    and git_command[0][1][-2]
    == "https://github.com/cxykevin/astrbot_plugin_libi18nject.git",
    str(git_command),
)
check(
    "已是完整地址时不再拼接",
    prerequisite_mod.clone_url("https://example.com/a.git")
    == "https://example.com/a.git"
    and prerequisite_mod.clone_url("cxykevin/astrbot_plugin_libi18nject")
    == "https://github.com/cxykevin/astrbot_plugin_libi18nject.git",
)

# 9.3 自动安装演练（本地 git 仓库，全程离线）
if shutil.which("git") is None:
    print("[SKIP] 本机没有 git，跳过自动安装演练")
else:
    seed = Path(tempfile.mkdtemp(prefix="librunner-prereq-seed-"))
    (seed / "metadata.yaml").write_text(f"name: {prereq.name}\n", encoding="utf-8")
    (seed / "main.py").write_text("# 前置占位\n", encoding="utf-8")

    async def _seed_repo():
        env = dict(os.environ)
        env.update(
            GIT_AUTHOR_NAME="selftest",
            GIT_AUTHOR_EMAIL="selftest@example.com",
            GIT_COMMITTER_NAME="selftest",
            GIT_COMMITTER_EMAIL="selftest@example.com",
        )
        for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "seed"]):
            process = await asyncio.create_subprocess_exec(*argv, cwd=str(seed), env=env)
            await process.wait()

    asyncio.run(_seed_repo())

    local_repo = prerequisite_mod.Prerequisite(prereq.name, str(seed))
    install_store = Path(tempfile.mkdtemp(prefix="librunner-prereq-install-")) / "plugins"
    install_result = asyncio.run(
        prerequisite_mod.install(local_repo, store=install_store, use_gh=False, timeout=30)
    )
    check("前置缺失时能自动安装", install_result.ok, install_result.message)
    check(
        "安装后前置就位",
        prerequisite_mod.is_installed(local_repo, store=install_store) is True,
    )
    repeat = asyncio.run(
        prerequisite_mod.install(local_repo, store=install_store, use_gh=False, timeout=30)
    )
    check("已安装时不再重复克隆", repeat.ok and repeat.source == "skip", repeat.message)

    broken = prerequisite_mod.Prerequisite(prereq.name, str(seed / "nope"))
    broken_store = Path(tempfile.mkdtemp(prefix="librunner-prereq-broken-")) / "plugins"
    broken_result = asyncio.run(
        prerequisite_mod.install(broken, store=broken_store, use_gh=False, timeout=30)
    )
    check(
        "来源不可用时安装失败并带回原因",
        not broken_result.ok and bool(broken_result.message),
        broken_result.message,
    )

# 9.4 不自动安装时如实报告未就绪
no_install_store = Path(tempfile.mkdtemp(prefix="librunner-prereq-none-")) / "plugins"
no_install = asyncio.run(
    prerequisite_mod.ensure(prereq, store=no_install_store, auto_install=False)
)
check(
    "未强制时只报告未就绪",
    no_install.ready is False and "未安装" in no_install.message,
    no_install.message,
)

# 9.5 就地激活前置（等价于「只重启插件」，不重启 AstrBot 主进程）
activation = asyncio.run(prerequisite_mod.activate(prereq, context=None))
if libi18n_registry is None:
    # 前置不在场（例如刚被搬走、内核还没来得及自动安装）：此时必须如实失败，
    # 不能假装激活成功，否则运行期会带着一个空的前置实例继续跑。
    check(
        "前置不在场时就地激活如实失败",
        not activation.ok and "无法导入" in activation.message,
        activation.message,
    )
else:
    check("就地激活前置插件", activation.ok, activation.message)
    check("激活后拿到前置实例", activation.instance is not None)
    if activation.instance is not None:
        asyncio.run(activation.instance.terminate())
        check("释放就地激活的前置实例", True)


# 9.6 安装失败 → 停止工作 + 交给内核在 WebUI 报告
async def _failing_initialize():
    fail_root = Path(tempfile.mkdtemp(prefix="librunner-prereq-fail-"))
    previous_root = os.environ.get("ASTRBOT_ROOT")
    os.environ["ASTRBOT_ROOT"] = str(fail_root)
    prerequisite_mod.reset_attempts()
    try:
        failing = LibrunnerPlugin(
            context=None,
            config={
                "enforce_prerequisite": True,
                "prerequisite_repo": str(fail_root / "missing-repo"),
            },
        )
        try:
            await failing.initialize()
        except prerequisite_mod.PrerequisiteError as exc:
            return str(exc)
        return None
    finally:
        if previous_root is None:
            os.environ.pop("ASTRBOT_ROOT", None)
        else:
            os.environ["ASTRBOT_ROOT"] = previous_root
        prerequisite_mod.reset_attempts()


failure_message = asyncio.run(_failing_initialize())
check(
    "装不上时抛 PrerequisiteError（由内核在 WebUI 报告）",
    bool(failure_message and "前置插件未就绪" in failure_message),
    str(failure_message),
)
check("失败时停止工作：未打内核补丁", api.get_status()["patched"] is False)
check("失败时停止工作：未注册执行器", not api.has_runner(ECHO_RUNNER_TYPE))

# 9.7 安装结果落盘（诊断用）
check(
    "自动安装结果会落盘记录",
    isinstance(prerequisite_mod.read_attempts(), dict)
    and prerequisite_mod.attempts_path().name == "prerequisite.json",
    str(prerequisite_mod.attempts_path()),
)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
