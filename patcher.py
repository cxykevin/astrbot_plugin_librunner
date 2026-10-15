"""把 librunner 注册表接入 AstrBot 内核的 monkey patch 实现。

本模块只做一件事：让 AstrBot 的第三方执行器子阶段（``agent_runner.runner_type``）
认识注入方注册进来的 runner，同时尽量复用内核自身的请求构造、流式输出、
看门狗与资源清理逻辑。它负责 4 个补丁点（WebUI 文案的 i18n 处理已经独立到姊妹插件
``astrbot_plugin_libi18nject``，见 ``main._refresh_i18n``）：

1. ``astrbot.core.config.agent_runner.AGENT_RUNNER_TYPES`` /
   ``AGENT_RUNNER_CONFIG_DEFAULTS`` —— 放行新的执行器类型，并提供配置模板，
   否则 ``normalize_agent_runner()`` 会在构建 pipeline 时直接抛 ``ValueError``。
2. ``CONFIG_METADATA_3`` 中 ``agent_runner.runner_type`` 的
   ``options`` / ``labels`` / ``runner_defaults`` —— 让新类型出现在 WebUI
   的执行器下拉框里；同时按内核内置分组的结构注入
   ``ai_group.metadata.<runner_type>_runner``，让该执行器在「AI -> Agent
   执行方式」页面上拥有自己的配置项（字段来自 ``config_default`` /
   ``config_metadata``）。
3. ``normalize_agent_runner()`` —— 当配置里残留了「本插件注入过、但当前无人
   注册」的类型时按 ``orphan_runner_policy`` 优雅降级（默认回退内置 Agent），
   避免注入方插件被卸载后 AstrBot 无法启动 AI 流程。
4. ``ThirdPartyAgentSubStage.process`` —— 唯一的分发点。内核用字面量 if/elif
   选择执行器且没有对外提供构造钩子，因此这里采用「只替换构造结果」的方式：
   透传内核原始实现，仅在本插件副本上把 ``runner_type`` 换成
   :class:`_RunnerTypeAlias`（与占位分支 deerflow 相等），再由
   :class:`_SlotFactoryProxy` 把内核将要构造的 runner 换成注入方的实例。
   这样流式/非流式、看门狗、错误聚合、Metric 上报等行为与内置第三方执行器
   完全一致，且无需复制内核代码。

所有补丁都记录原始对象，``revert()`` 可完整还原；``apply()`` 是幂等的，并能
回收上一次实例残留的补丁（插件热重载或上次未正常 ``terminate`` 的情况）。
"""

from __future__ import annotations

import copy
import functools
import importlib
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from . import api

_AGENT_RUNNER_MODULE = "astrbot.core.config.agent_runner"
_DEFAULT_CONFIG_MODULE = "astrbot.core.config.default"
_THIRD_PARTY_MODULE = (
    "astrbot.core.pipeline.process_stage.method.agent_sub_stages.third_party"
)

#: 被借用构造流程的内置第三方执行器类型（内核 dispatch 的最后一个分支）。
_SLOT_RUNNER_TYPE = "deerflow"

_PATCH_FLAG = "_librunner_process_patched"
_ORIGINAL_PROCESS_ATTR = "_librunner_original_process"
_ORIGINAL_SLOT_ATTR = "_librunner_original_slot_runner"
_SLOT_PROXY_FLAG = "_librunner_slot_proxy"

#: runner 必须具备的方法（``close`` 为可选，内核用鸭子类型探测）。
_REQUIRED_RUNNER_METHODS = ("reset", "step_until_done", "done", "get_final_llm_resp")

#: ``normalize_agent_runner`` 命中残留类型时的策略。
ORPHAN_FALLBACK = "fallback_local"
ORPHAN_STRICT = "strict"
ORPHAN_POLICIES = (ORPHAN_FALLBACK, ORPHAN_STRICT)


@dataclass(frozen=True)
class _Dispatch:
    """一次请求的分发上下文。"""

    spec: dict[str, Any]
    event: Any


_active_dispatch: ContextVar[_Dispatch | None] = ContextVar(
    "librunner_active_dispatch", default=None
)


class _RunnerTypeAlias(str):
    """既是真实执行器类型、又能匹配内核占位分支的字符串。

    内核用 ``self.runner_type == DEERFLOW_PROVIDER_TYPE`` 之类的字面量比较挑
    执行器。本类型只作为本次请求所用 ``self`` 副本上的 ``runner_type``，使该
    比较成立，从而复用内核 deerflow 分支的完整流程；``str`` 的取值仍是真实
    类型，因此 ``Metric.upload(model_name=self.runner_type)`` 上报的名字不失真。
    """

    __slots__ = ("_slot_type",)

    def __new__(cls, value: str, slot_type: str) -> _RunnerTypeAlias:
        instance = super().__new__(cls, value)
        instance._slot_type = slot_type
        return instance

    def __eq__(self, other: object) -> bool:
        if str.__eq__(self, other) is True:
            return True
        return isinstance(other, str) and other == self._slot_type

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return str.__hash__(self)


class _SlotFactoryProxy:
    """替换内核 dispatch 里被调用的 runner 类。

    内核写的是 ``runner = DeerFlowAgentRunner[AstrAgentContext]()``，即
    「取下标得到可调用对象，再实例化」。这里拦截下标取值：
    处于 librunner 分发上下文时返回注入方 factory，否则原样返回内核类。
    上下文用 ``ContextVar`` 承载，所以并发请求互不干扰。
    """

    def __init__(self, real_factory: Any) -> None:
        self._real = real_factory
        setattr(self, _SLOT_PROXY_FLAG, True)

    def __getitem__(self, item: Any) -> Any:
        dispatch = _active_dispatch.get()
        if dispatch is None:
            return self._real[item]
        return functools.partial(_build_runner, dispatch)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        dispatch = _active_dispatch.get()
        if dispatch is None:
            return self._real(*args, **kwargs)
        return _build_runner(dispatch)

    def __getattr__(self, name: str) -> Any:
        # 兜底：内核若通过其它方式访问该类，仍透传。
        return getattr(self.__dict__["_real"], name)


class _RunnerFacade:
    """给注入方 runner 补上 ``reset()`` 额外参数能力的薄代理。

    除 ``reset`` 外所有属性访问都转发给注入方的 runner 实例，因此内核的鸭子
    类型调用（``step_until_done`` / ``done`` / ``get_final_llm_resp`` /
    ``close``）行为不变。
    """

    def __init__(self, inner: Any, extra_reset_kwargs: dict[str, Any]) -> None:
        self.__dict__["_inner"] = inner
        self.__dict__["_extra_reset_kwargs"] = extra_reset_kwargs

    async def reset(self, **kwargs: Any) -> Any:
        merged = dict(kwargs)
        merged.update(self.__dict__["_extra_reset_kwargs"])
        return await self.__dict__["_inner"].reset(**merged)

    def __getattr__(self, name: str) -> Any:
        try:
            inner = self.__dict__["_inner"]
        except KeyError as exc:  # pragma: no cover - 初始化失败才会走到
            raise AttributeError(name) from exc
        return getattr(inner, name)


def _resolve_reset_kwargs(spec: dict[str, Any], event: Any) -> dict[str, Any]:
    """解析注入方声明的 ``reset()`` 额外参数。

    Args:
        spec: 注册项。
        event: 当前消息事件。

    Returns:
        追加到 ``reset()`` 的关键字参数字典。

    Raises:
        RuntimeError: 注入方的 ``reset_kwargs`` 可调用对象抛错。
        TypeError: 注入方的 ``reset_kwargs`` 返回值不是 Mapping。
    """
    extra = spec.get("reset_kwargs")
    if extra is None:
        return {}
    if isinstance(extra, Mapping):
        return dict(extra)

    runner_type = spec["runner_type"]
    try:
        resolved = extra(event) if spec.get("reset_kwargs_takes_event") else extra()
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise RuntimeError(
            f"librunner: 执行器 {runner_type!r} 的 reset_kwargs 调用失败: {exc}"
        ) from exc
    if not isinstance(resolved, Mapping):
        raise TypeError(
            f"librunner: 执行器 {runner_type!r} 的 reset_kwargs 必须返回字典，"
            f"实际为 {type(resolved).__name__}"
        )
    return dict(resolved)


def _build_runner(dispatch: _Dispatch) -> _RunnerFacade:
    """调用注入方的 factory 并校验 runner 契约。

    Args:
        dispatch: 当前分发上下文。

    Returns:
        包装后的 runner。

    Raises:
        RuntimeError: factory 调用失败。
        TypeError: 返回对象缺少必需的 runner 方法。
    """
    spec = dispatch.spec
    runner_type = spec["runner_type"]
    owner = spec["owner"]
    try:
        inner = (
            spec["factory"](dispatch.event)
            if spec["factory_takes_event"]
            else spec["factory"]()
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise RuntimeError(
            f"librunner: 执行器 {runner_type!r}（由插件 {owner!r} 注册）的 factory "
            f"调用失败: {exc}"
        ) from exc

    missing = [
        name
        for name in _REQUIRED_RUNNER_METHODS
        if not callable(getattr(inner, name, None))
    ]
    if missing:
        raise TypeError(
            f"librunner: 执行器 {runner_type!r}（由插件 {owner!r} 注册）返回的"
            f"{type(inner).__name__} 缺少可调用方法 {missing}；它需要满足 "
            "BaseAgentRunner 契约（reset / step_until_done / done / "
            "get_final_llm_resp，可选 close）"
        )

    extra = _resolve_reset_kwargs(spec, dispatch.event)
    logger.debug(
        "librunner: 分发执行器 %s（owner=%s, reset 额外参数=%s）",
        runner_type,
        owner,
        sorted(extra),
    )
    return _RunnerFacade(inner, extra)


async def _patched_process(self: Any, event: Any, provider_wake_prefix: str):
    """``ThirdPartyAgentSubStage.process`` 的替换实现。

    未注册的类型原样透传内核实现；已注册的类型在本次调用使用的副本上做类型
    别名，再交给内核实现完成其余全部工作。

    Args:
        self: 子阶段实例。
        event: 消息事件。
        provider_wake_prefix: provider 唤醒前缀。

    Yields:
        内核 ``process`` 的产出（调度器进度信号）。
    """
    original = getattr(type(self), _ORIGINAL_PROCESS_ATTR, None)
    if not callable(original):
        logger.error(
            "librunner: 找不到内核 process 的原始实现，本次请求直接透传；"
            "请重载 librunner 插件。"
        )
        return

    spec = api._get_spec(getattr(self, "runner_type", None))
    if spec is None:
        async for item in original(self, event, provider_wake_prefix):
            yield item
        return

    shim = copy.copy(self)
    shim.runner_type = _RunnerTypeAlias(spec["runner_type"], _SLOT_RUNNER_TYPE)
    token = _active_dispatch.set(_Dispatch(spec=spec, event=event))
    try:
        async for item in original(shim, event, provider_wake_prefix):
            yield item
    finally:
        _active_dispatch.reset(token)


class CorePatcher:
    """管理 librunner 对 AstrBot 内核的全部补丁。"""

    def __init__(self, *, orphan_policy: str = ORPHAN_FALLBACK) -> None:
        """初始化补丁管理器。

        Args:
            orphan_policy: 残留执行器类型的处理策略，见 :data:`ORPHAN_POLICIES`。
        """
        self._orphan_policy = (
            orphan_policy if orphan_policy in ORPHAN_POLICIES else ORPHAN_FALLBACK
        )
        self._applied = False
        self._agent_runner_module: Any = None
        self._third_party_module: Any = None
        self._base_runner_types: tuple[str, ...] = ()
        self._added_default_keys: set[str] = set()
        self._schema_backup: dict[str, Any] | None = None
        self._normalize_original: Any = None
        self._rebound_modules: list[tuple[Any, str, Any, Any]] = []
        self._original_process: Any = None
        self._original_slot: Any = None

    @property
    def applied(self) -> bool:
        """内核补丁是否已生效。"""
        return self._applied

    @property
    def orphan_policy(self) -> str:
        """当前使用的残留执行器处理策略。"""
        return self._orphan_policy

    def apply(self) -> bool:
        """应用全部内核补丁（幂等）。

        Returns:
            True 表示补丁已生效（或此前已生效）；False 表示 AstrBot 版本不兼容
            或补丁过程中出错（错误会写入 :func:`api.get_status`）。
        """
        if self._applied:
            return True

        try:
            agent_runner_module = importlib.import_module(_AGENT_RUNNER_MODULE)
            third_party_module = importlib.import_module(_THIRD_PARTY_MODULE)
        except Exception as exc:  # noqa: BLE001 - 版本不兼容时给出明确错误
            self._fail(f"无法导入 AstrBot 内核模块: {exc}")
            return False

        missing = [
            f"{_AGENT_RUNNER_MODULE}.{name}"
            for name in (
                "AGENT_RUNNER_TYPES",
                "AGENT_RUNNER_CONFIG_DEFAULTS",
                "normalize_agent_runner",
            )
            if not hasattr(agent_runner_module, name)
        ] + [
            f"{_THIRD_PARTY_MODULE}.{name}"
            for name in (
                "ThirdPartyAgentSubStage",
                "DeerFlowAgentRunner",
                "DEERFLOW_PROVIDER_TYPE",
            )
            if not hasattr(third_party_module, name)
        ]
        if missing:
            self._fail(
                f"AstrBot 内核缺少本插件依赖的符号: {missing}；"
                "当前 AstrBot 版本的第三方执行器实现可能已变更，注入接口不可用。"
            )
            return False

        self._agent_runner_module = agent_runner_module
        self._third_party_module = third_party_module
        self._base_runner_types = tuple(agent_runner_module.AGENT_RUNNER_TYPES)

        try:
            self._sync_runner_registry()
            self._sync_schema()
            self._patch_normalize()
            self._patch_dispatch()
        except Exception as exc:  # noqa: BLE001 - 失败时回滚，避免半应用状态
            logger.error("librunner: 应用内核补丁失败，正在回滚: %s", exc, exc_info=True)
            self.revert()
            self._fail(f"应用内核补丁失败: {exc}")
            return False

        self._applied = True
        api._register_listener(self.sync)
        api._set_patch_state(True, None)
        logger.info(
            "librunner: 内核补丁已应用（当前已注册执行器: %s）",
            api.registered_types() or "无",
        )
        return True

    def revert(self) -> None:
        """撤销全部内核补丁，恢复到 AstrBot 原始行为。"""
        try:
            api._unregister_listener(self.sync)
        except Exception:  # noqa: BLE001 - 撤销阶段尽力而为
            logger.debug("librunner: 取消注册表监听失败", exc_info=True)

        self._restore_dispatch()
        self._restore_normalize()
        self._restore_schema()
        self._restore_runner_registry()

        self._applied = False
        api._set_patch_state(False, None)
        logger.info("librunner: 内核补丁已撤销。")

    def sync(self) -> None:
        """注册表变化后同步执行器类型与 WebUI 元数据。"""
        if not self._applied:
            return
        try:
            self._sync_runner_registry()
            self._sync_schema()
        except Exception:  # noqa: BLE001 - 同步失败不影响已注册 runner 的运行
            logger.error("librunner: 同步执行器注册信息到内核失败", exc_info=True)

    def _fail(self, message: str) -> None:
        """记录补丁失败状态。

        Args:
            message: 面向用户的错误说明。
        """
        logger.error("librunner: %s", message)
        api._set_patch_state(False, message)

    def _sync_runner_registry(self) -> None:
        """把注入的类型并入 ``AGENT_RUNNER_TYPES`` 与配置默认值。"""
        module = self._agent_runner_module
        if module is None:
            return

        extra_types = tuple(
            runner_type
            for runner_type in api.registered_types()
            if runner_type not in self._base_runner_types
        )
        # 只重绑 AGENT_RUNNER_TYPES：normalize_agent_runner 在函数体内读取本模块
        # 全局变量，重绑即可生效。刻意不动 THIRD_PARTY_AGENT_RUNNER_TYPES ——
        # migra_helper 会拿它去查内置类型的字段映射表。
        module.AGENT_RUNNER_TYPES = self._base_runner_types + extra_types

        defaults = module.AGENT_RUNNER_CONFIG_DEFAULTS
        for spec in api._iter_specs():
            runner_type = spec["runner_type"]
            defaults[runner_type] = copy.deepcopy(spec["config_default"])
            self._added_default_keys.add(runner_type)
        for runner_type in list(self._added_default_keys):
            if runner_type not in extra_types:
                defaults.pop(runner_type, None)
                self._added_default_keys.discard(runner_type)

    def _restore_runner_registry(self) -> None:
        """还原 ``AGENT_RUNNER_TYPES`` 与配置默认值。"""
        module = self._agent_runner_module
        if module is None:
            return
        for runner_type in list(self._added_default_keys):
            module.AGENT_RUNNER_CONFIG_DEFAULTS.pop(runner_type, None)
        self._added_default_keys.clear()
        if self._base_runner_types:
            module.AGENT_RUNNER_TYPES = self._base_runner_types

    def _sync_schema(self) -> None:
        """把注入的类型并入 WebUI 的执行器下拉框元数据。

        AstrBot 的 ``CONFIG_METADATA_3`` 是模块级字典，面板在请求时读取它，
        因此在插件初始化阶段原地修改即可生效。
        """
        try:
            module = importlib.import_module(_DEFAULT_CONFIG_MODULE)
        except Exception as exc:  # noqa: BLE001 - 元数据缺失只影响 UI
            logger.warning("librunner: 无法导入配置元数据模块: %s", exc)
            return

        metadata = getattr(module, "CONFIG_METADATA_3", None)
        item = (
            ((((metadata or {}).get("ai_group") or {}).get("metadata") or {}).get(
                "agent_runner"
            ) or {})
            .get("items", {})
            .get("agent_runner.runner_type")
        )
        if not isinstance(item, dict):
            logger.warning(
                "librunner: 未找到 agent_runner.runner_type 的配置元数据，"
                "WebUI 执行器下拉框不会显示注入的执行器（功能本身不受影响）。"
            )
            return

        if self._schema_backup is None:
            self._schema_backup = {
                "item": item,
                "options": copy.deepcopy(item.get("options")),
                "labels": copy.deepcopy(item.get("labels")),
                "runner_defaults": copy.deepcopy(item.get("runner_defaults")),
                "groups_parent": _runner_groups_parent(metadata),
                "groups": {},
            }
        backup = self._schema_backup

        base_options = [value for value in (backup["options"] or []) if isinstance(value, str)]
        options = list(base_options)
        labels = [value for value in (backup["labels"] or []) if isinstance(value, str)]
        # 前端按下标取 label，缺失时回退为原始值；先补齐内置项的对齐关系。
        while len(labels) < len(base_options):
            labels.append(base_options[len(labels)])
        defaults = dict(backup["runner_defaults"] or {})

        for spec in api._iter_specs():
            runner_type = spec["runner_type"]
            if runner_type not in options:
                options.append(runner_type)
            labels.append(spec["label"] or runner_type)
            defaults[runner_type] = copy.deepcopy(spec["config_default"])

        item["options"] = options
        item["labels"] = labels
        item["runner_defaults"] = defaults
        self._sync_runner_groups(backup)

    def _sync_runner_groups(self, backup: dict[str, Any]) -> None:
        """注入 / 刷新 / 回收「执行器配置分组」。

        面板在渲染第三方执行器的配置项时，读取的是
        ``ai_group.metadata.<runner_type>_runner``（见 AiConfigPanel 的
        ``thirdPartyRunnerGroup``）。这里按内核内置分组的结构注入等价元数据，
        因此注入的执行器也能在「AI -> Agent 执行方式」页面里配置
        ``agent_runner.config`` 中的字段。

        Args:
            backup: :meth:`_sync_schema` 建立的状态备份（携带 ``groups_parent``
                与已注入分组的原值）。
        """
        groups_parent = backup.get("groups_parent")
        if not isinstance(groups_parent, dict):
            return
        recorded: dict[str, Any] = backup["groups"]

        wanted: dict[str, dict[str, Any]] = {}
        for spec in api._iter_specs():
            group = _build_runner_group(spec)
            if group is not None:
                wanted[f"{spec['runner_type']}_runner"] = group

        # 已注入、但当前不该再存在的分组（执行器被注销或不再声明字段）：还原。
        for key in [key for key in recorded if key not in wanted]:
            _restore_runner_group(groups_parent, key, recorded.pop(key))
        for key, group in wanted.items():
            if key not in recorded:
                recorded[key] = groups_parent.get(key)
            groups_parent[key] = group

    def _restore_schema(self) -> None:
        """还原 WebUI 执行器下拉框元数据。"""
        backup = self._schema_backup
        if backup is None:
            return
        item = backup.get("item")
        try:
            if isinstance(item, dict):
                item["options"] = backup["options"]
                item["labels"] = backup["labels"]
                item["runner_defaults"] = backup["runner_defaults"]
            groups_parent = backup.get("groups_parent")
            if isinstance(groups_parent, dict):
                for key, original in (backup.get("groups") or {}).items():
                    _restore_runner_group(groups_parent, key, original)
        except Exception:  # noqa: BLE001 - 元数据被外部替换时跳过
            logger.debug("librunner: 还原配置元数据失败", exc_info=True)
        self._schema_backup = None

    def _patch_normalize(self) -> None:
        """给 ``normalize_agent_runner`` 加上残留类型的优雅降级。"""
        module = self._agent_runner_module
        if module is None:
            return

        original = module.normalize_agent_runner
        if getattr(original, "_librunner_wrapped", False):
            original = getattr(original, "_librunner_original", original)
        policy = self._orphan_policy

        def _normalize(agent_runner: Any) -> dict[str, Any]:
            try:
                return original(agent_runner)
            except ValueError:
                runner_type = (
                    agent_runner.get("runner_type")
                    if isinstance(agent_runner, dict)
                    else None
                )
                if policy == ORPHAN_FALLBACK and _is_orphan_runner_type(runner_type):
                    logger.error(
                        "librunner: 配置中的执行器 %r 属于本插件注入过的类型，但当前"
                        "没有任何插件注册它（注入方插件可能已被禁用或卸载）。已按 "
                        "orphan_runner_policy=fallback_local 回退到内置 Agent；"
                        "请在 WebUI 的「AI -> Agent 执行方式」中重新选择执行器。",
                        runner_type,
                    )
                    return original({"runner_type": "local", "config": {}})
                raise

        _normalize._librunner_wrapped = True  # type: ignore[attr-defined]
        _normalize._librunner_original = original  # type: ignore[attr-defined]

        module.normalize_agent_runner = _normalize
        self._normalize_original = original
        self._rebind_imported("normalize_agent_runner", original, _normalize)

    def _restore_normalize(self) -> None:
        """还原 ``normalize_agent_runner`` 及其导入引用。"""
        module = self._agent_runner_module
        if module is not None and self._normalize_original is not None:
            module.normalize_agent_runner = self._normalize_original
        for holder, name, new_value, old_value in reversed(self._rebound_modules):
            try:
                if getattr(holder, name, None) is new_value:
                    setattr(holder, name, old_value)
            except Exception:  # noqa: BLE001 - 模块可能已卸载
                continue
        self._rebound_modules.clear()
        self._normalize_original = None

    def _rebind_imported(self, name: str, old_value: Any, new_value: Any) -> None:
        """把已 ``from ... import`` 过该名字的模块重新绑定到新函数。

        ``agent_request`` / ``migra_helper`` 等在导入时就绑定了函数对象，只改
        源模块属性对它们无效；而之后才导入的模块（如 dashboard 的
        ``config_service``）会自动拿到新函数。

        Args:
            name: 模块属性名。
            old_value: 原始对象。
            new_value: 替换后的对象。
        """
        for holder in list(sys.modules.values()):
            if holder is None:
                continue
            try:
                if getattr(holder, name, None) is old_value:
                    setattr(holder, name, new_value)
                    self._rebound_modules.append((holder, name, new_value, old_value))
            except Exception:  # noqa: BLE001 - 个别模块属性只读或动态生成
                continue

    def _patch_dispatch(self) -> None:
        """替换 third_party 子阶段的 runner 构造入口。"""
        module = self._third_party_module
        if module is None:
            return

        sub_stage_cls = module.ThirdPartyAgentSubStage
        stale_original = getattr(sub_stage_cls, _ORIGINAL_PROCESS_ATTR, None)
        if getattr(sub_stage_cls, _PATCH_FLAG, False) and callable(stale_original):
            # 上一次实例（插件热重载或异常退出）残留的补丁，先还原再重新打。
            sub_stage_cls.process = stale_original
            logger.debug("librunner: 发现残留的旧补丁，已先恢复内核原始实现。")

        self._original_process = sub_stage_cls.process
        setattr(sub_stage_cls, _ORIGINAL_PROCESS_ATTR, self._original_process)
        setattr(sub_stage_cls, _PATCH_FLAG, True)
        sub_stage_cls.process = _patched_process

        stale_slot = getattr(module, _ORIGINAL_SLOT_ATTR, None)
        current_slot = getattr(module, "DeerFlowAgentRunner", None)
        if stale_slot is not None and getattr(current_slot, _SLOT_PROXY_FLAG, False):
            module.DeerFlowAgentRunner = stale_slot
        self._original_slot = module.DeerFlowAgentRunner
        setattr(module, _ORIGINAL_SLOT_ATTR, self._original_slot)
        module.DeerFlowAgentRunner = _SlotFactoryProxy(self._original_slot)

    def _restore_dispatch(self) -> None:
        """还原 third_party 子阶段的 process 与 runner 类。"""
        module = self._third_party_module
        if module is None:
            return

        sub_stage_cls = getattr(module, "ThirdPartyAgentSubStage", None)
        if sub_stage_cls is not None:
            original = self._original_process or getattr(
                sub_stage_cls, _ORIGINAL_PROCESS_ATTR, None
            )
            if callable(original):
                sub_stage_cls.process = original
            for attribute in (_PATCH_FLAG, _ORIGINAL_PROCESS_ATTR):
                try:
                    delattr(sub_stage_cls, attribute)
                except AttributeError:
                    pass

        slot_original = self._original_slot or getattr(module, _ORIGINAL_SLOT_ATTR, None)
        if slot_original is not None:
            module.DeerFlowAgentRunner = slot_original
            try:
                delattr(module, _ORIGINAL_SLOT_ATTR)
            except AttributeError:
                pass

        self._original_process = None
        self._original_slot = None


def _is_orphan_runner_type(runner_type: Any) -> bool:
    """判断某个执行器类型是否为「本插件注入过、但当前无人注册」。

    Args:
        runner_type: 配置中的执行器类型。

    Returns:
        是否需要按残留类型处理。
    """
    if not isinstance(runner_type, str) or not runner_type:
        return False
    if api.has_runner(runner_type):
        return False
    return runner_type in api.known_injected_types()


def _runner_groups_parent(metadata: Any) -> dict[str, Any] | None:
    """取出 ``ai_group.metadata``：内置执行器配置分组的父级字典。

    Args:
        metadata: ``CONFIG_METADATA_3``。

    Returns:
        可变字典（注入分组直接写在这里）；结构不符时返回 None。
    """
    if not isinstance(metadata, Mapping):
        return None
    ai_group = metadata.get("ai_group")
    if not isinstance(ai_group, Mapping):
        return None
    groups = ai_group.get("metadata")
    return groups if isinstance(groups, dict) else None


def _is_injected_group(key: str, value: Any) -> bool:
    """判断某个配置分组是不是本插件注入的执行器分组。

    用于区分「本插件注入的分组」与「内核自带的分组」：前者在回收时应当直接
    删除，后者（理论上不会与注入类型同名）应原样还原。

    Args:
        key: 分组名（``<runner_type>_runner``）。
        value: 该分组的当前内容。

    Returns:
        是否是本插件注入的执行器分组。
    """
    if not key.endswith("_runner") or not isinstance(value, Mapping):
        return False
    runner_type = key[: -len("_runner")]
    condition = value.get("condition")
    if not isinstance(condition, Mapping):
        return False
    if condition.get("agent_runner.runner_type") != runner_type:
        return False
    items = value.get("items")
    if not isinstance(items, Mapping):
        return False
    return all(str(item_key).startswith("agent_runner.config.") for item_key in items)


def _restore_runner_group(groups_parent: dict[str, Any], key: str, original: Any) -> None:
    """把注入的配置分组还原为注入前的状态。

    若该位置当前是本插件注入的分组（常见于插件热重载：上一个实例注入后未
    ``revert`` 就退场），直接删除，而不是把「上一次注入的内容」当成原值写回，
    否则执行器注销后 WebUI 上会残留它的配置项。

    Args:
        groups_parent: ``ai_group.metadata``。
        key: 分组名（``<runner_type>_runner``）。
        original: 注入前的值；None 表示该分组原本不存在。
    """
    if original is None or _is_injected_group(key, groups_parent.get(key)):
        groups_parent.pop(key, None)
    else:
        groups_parent[key] = original


def _build_runner_group(spec: dict[str, Any]) -> dict[str, Any] | None:
    """按内核内置分组的结构，构造某个注入执行器的 WebUI 配置分组。

    Args:
        spec: 注册项（见 :func:`api.register_runner`）。

    Returns:
        可直接放进 ``ai_group.metadata`` 的分组字典；该执行器没有任何配置字段
        时返回 None —— 此时面板上无需展示配置项。
    """
    defaults = spec.get("config_default")
    defaults = defaults if isinstance(defaults, Mapping) else {}
    declared = spec.get("config_metadata")
    declared = declared if isinstance(declared, Mapping) else {}

    fields = list(defaults) + [field for field in declared if field not in defaults]
    if not fields:
        return None

    items: dict[str, Any] = {}
    for field in fields:
        entry = dict(declared.get(field) or {})
        entry.setdefault("description", field)
        entry.setdefault("type", _infer_field_type(defaults.get(field)))
        items[f"agent_runner.config.{field}"] = entry

    runner_type = spec["runner_type"]
    return {
        "description": spec.get("label") or runner_type,
        "type": "object",
        "condition": {
            "provider_settings.enable": True,
            "agent_runner.runner_type": runner_type,
        },
        "items": items,
    }


def _infer_field_type(value: Any) -> str:
    """按默认值推断 WebUI 字段类型（取值风格与内核 metadata 的 ``type`` 一致）。

    Args:
        value: ``config_default`` 中该字段的默认值。

    Returns:
        字段类型名。
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "dict"
    if isinstance(value, (list, tuple)):
        return "list"
    return "string"
