"""librunner 的公共注入接口（供其它 AstrBot 插件调用）。

其它插件通过本模块把自己实现的 Agent 执行器
（``astrbot.core.agent.runners.base.BaseAgentRunner`` 的实现）注册成 AstrBot
的一个新的 ``agent_runner.runner_type`` 选项，从而出现在 WebUI 的
「AI -> Agent 执行方式 -> 执行器」下拉框中，无需修改 AstrBot 核心代码。

用法（注入方插件）::

    from data.plugins.astrbot_plugin_librunner.api import (
        register_runner,
        unregister_runner,
    )

    class MyPlugin(Star):
        async def initialize(self):
            register_runner(
                "myagent",
                factory=lambda: MyAgentRunner[AstrAgentContext](),
                config_default={"api_base": "http://127.0.0.1:8080", "timeout": 60},
                config_metadata={
                    "api_base": "服务地址",
                    "timeout": {"description": "超时时间（秒）", "type": "int"},
                },
                label="MyAgent",
                owner="astrbot_plugin_myagent",
            )

        async def terminate(self):
            unregister_runner("myagent", owner="astrbot_plugin_myagent")

刻意的设计取舍：

* 注册表放在一个进程级共享模块中（见 :func:`_shared_state`），而不是本模块的
  全局变量。这样两个插件的加载顺序不影响注册结果，且 librunner 自身被热重载
  或更新（模块被重新导入）后，注入方已完成的注册依然有效。
* 同一个 owner 重复注册同一个 ``runner_type`` 视为幂等覆盖（插件重载常见），
  不同 owner 抢占则必须显式传 ``replace=True``。
* 注入过的类型会落盘记录（``data/plugin_data/astrbot_plugin_librunner/``），
  当注入方插件被禁用/卸载导致配置里残留该类型时，librunner 可以让 AstrBot
  优雅降级到内置 Agent，而不是在构建 pipeline 时抛 ``ValueError``。
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import sys
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

#: 插件名（必须与 metadata.yaml 中的 name、插件目录名一致）。
PLUGIN_NAME = "astrbot_plugin_librunner"

#: AstrBot 内置的执行器类型。注入方不允许覆盖这些类型。
BUILTIN_RUNNER_TYPES: tuple[str, ...] = (
    "local",
    "dify",
    "coze",
    "dashscope",
    "deerflow",
)

#: 进程级共享状态的载体模块名。刻意不带 ``data.plugins.`` 前缀，这样
#: AstrBot 卸载/重载插件时清理 ``sys.modules`` 不会把它一并删掉。
_SHARED_MODULE_NAME = "astrbot_plugin_librunner_shared_state"

#: 落盘文件名，用于记录「历史上注入过的执行器类型」。
_STATE_FILE_NAME = "injected_runners.json"

_RUNNER_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class RunnerInfo:
    """已注册执行器的只读快照。"""

    runner_type: str
    """执行器类型，即 ``agent_runner.runner_type`` 的取值。"""

    owner: str
    """注册方标识（通常是注入方插件目录名）。"""

    label: str
    """展示名，用于 WebUI 下拉框的备选文案。"""

    description: str
    """描述文本，仅用于日志/诊断。"""

    config_default: dict[str, Any]
    """该执行器在 ``agent_runner.config`` 中的默认配置模板。"""

    config_metadata: dict[str, dict[str, Any]]
    """各配置字段在 WebUI 上的展示元数据（``description`` / ``type`` / ``hint`` 等）。"""

    def unregister(self) -> bool:
        """注销本执行器，等价于 :func:`unregister_runner`。"""
        return unregister_runner(self.runner_type, owner=self.owner)


def _shared_state() -> dict[str, Any]:
    """返回进程级共享状态。

    共享状态挂在一个独立的模块对象上而不是本模块的全局变量上：AstrBot 重载
    插件时会删除 ``data.plugins.*`` 下的模块，本模块的全局变量会随之重建，而
    注入方插件可能仍持有旧模块对象的引用。放在共享模块里可以保证两边始终读写
    同一份注册表。

    Returns:
        ``{"runners": dict, "listeners": list, "patched": bool,
        "patch_error": str | None, "persisted": dict}``
    """
    module = sys.modules.get(_SHARED_MODULE_NAME)
    if not isinstance(module, types.ModuleType):
        module = types.ModuleType(_SHARED_MODULE_NAME)
        sys.modules[_SHARED_MODULE_NAME] = module

    state = getattr(module, "state", None)
    if not isinstance(state, dict):
        state = {}
        module.state = state  # type: ignore[attr-defined]

    state.setdefault("runners", {})
    state.setdefault("listeners", [])
    state.setdefault("patched", False)
    state.setdefault("patch_error", None)
    state.setdefault("persisted", _read_persisted(state))
    return state


def _accepts_event(func: Any) -> bool:
    """判断可调用对象是否接受第一个位置参数（事件对象）。

    Args:
        func: 待检查的可调用对象。

    Returns:
        True 表示调用时应传入 ``event``，False 表示应以零参数调用。
    """
    if not callable(func):
        return False
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        # 内建函数、部分 C 实现或泛型别名无法取签名，按零参数处理。
        return False

    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if (
            parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and parameter.default is inspect.Parameter.empty
        ):
            return True
    return False


def _caller_owner() -> str:
    """从调用栈推断注入方插件名。

    Returns:
        插件目录名（``data.plugins.<name>.xxx`` / ``astrbot.builtin_stars.<name>.xxx``）
        或子模块名；无法判断时返回 ``"unknown"``。
    """
    frame = sys._getframe(2)
    while frame is not None:
        module_name = frame.f_globals.get("__name__")
        if isinstance(module_name, str):
            parts = module_name.split(".")
            for flag in ("plugins", "builtin_stars"):
                if flag in parts:
                    index = parts.index(flag)
                    if index + 1 < len(parts):
                        return parts[index + 1]
            if parts and parts[0] != __name__:
                return parts[0]
        frame = frame.f_back
    return "unknown"


def _state_file() -> Path | None:
    """返回持久化文件路径；AstrBot 路径不可用时返回 None。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path())
    except Exception:  # noqa: BLE001 - 路径工具不可用时静默降级
        return None
    return base / PLUGIN_NAME / _STATE_FILE_NAME


def _read_persisted(state: dict[str, Any]) -> dict[str, Any]:
    """读取落盘的「历史注入类型」记录，读取失败时返回空表。"""
    path = _state_file()
    if path is None or not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("librunner: 读取注入记录失败: %s", exc)
        return {}
    runners = payload.get("runners") if isinstance(payload, dict) else None
    if not isinstance(runners, dict):
        return {}
    result: dict[str, Any] = {}
    for runner_type, entry in runners.items():
        if isinstance(runner_type, str) and isinstance(entry, dict):
            result[runner_type] = {
                "owner": entry.get("owner", "unknown"),
                "label": entry.get("label", runner_type),
            }
    return result


def _persist(state: dict[str, Any]) -> None:
    """把当前注册表与历史记录写盘（尽力而为，失败只记日志）。"""
    runners: dict[str, Any] = dict(state.get("persisted") or {})
    for runner_type, spec in state["runners"].items():
        runners[runner_type] = {
            "owner": spec.get("owner", "unknown"),
            "label": spec.get("label", runner_type),
        }
    state["persisted"] = runners

    path = _state_file()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "runners": runners}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("librunner: 写入注入记录失败: %s", exc)


def _notify() -> None:
    """通知监听者（通常是内核补丁）注册表已变化。"""
    state = _shared_state()
    for listener in list(state["listeners"]):
        if not callable(listener):
            continue
        try:
            listener()
        except Exception:  # noqa: BLE001 - 单个监听者失败不应影响注册流程
            logger.error("librunner: 注册表监听者执行失败", exc_info=True)


def _normalize_config_metadata(
    runner_type: str,
    config_metadata: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """校验并归一化 ``register_runner`` 的 ``config_metadata`` 参数。

    Args:
        runner_type: 执行器类型名，仅用于错误信息。
        config_metadata: 注入方传入的字段元数据。

    Returns:
        字段名到元数据字典的映射；入参为 None 时返回空表。

    Raises:
        TypeError: 入参不是 Mapping，或某个字段的值既不是字符串也不是 Mapping。
        ValueError: 字段名不是非空字符串。
    """
    if config_metadata is None:
        return {}
    if not isinstance(config_metadata, Mapping):
        raise TypeError("config_metadata 必须是字典")

    normalized: dict[str, dict[str, Any]] = {}
    for field, meta in config_metadata.items():
        if not isinstance(field, str) or not field.strip():
            raise ValueError(
                f"config_metadata 的字段名必须是非空字符串（执行器 {runner_type!r}）"
            )
        if isinstance(meta, str):
            normalized[field] = {"description": meta}
        elif isinstance(meta, Mapping):
            normalized[field] = dict(meta)
        else:
            raise TypeError(
                f"config_metadata[{field!r}] 必须是描述字符串或字典，"
                f"实际为 {type(meta).__name__}"
            )
    return normalized


def register_runner(
    runner_type: str,
    factory: Callable[..., Any],
    *,
    config_default: Mapping[str, Any] | None = None,
    config_metadata: Mapping[str, Any] | None = None,
    label: str | None = None,
    description: str = "",
    reset_kwargs: Callable[..., Mapping[str, Any]] | Mapping[str, Any] | None = None,
    owner: str | None = None,
    replace: bool = False,
) -> RunnerInfo:
    """注册一个自定义 Agent 执行器。

    Args:
        runner_type: 执行器类型名，将作为 ``agent_runner.runner_type`` 的取值。
            只能由字母、数字、``_``、``.``、``-`` 组成，且不能是内置类型。
        factory: 构造 runner 实例的可调用对象，应返回 ``BaseAgentRunner``
            的实现（需具备 ``reset`` / ``step_until_done`` / ``done`` /
            ``get_final_llm_resp``，可选 ``close``）。可接受 0 个参数，或 1 个
            位置参数（当前消息事件 ``AstrMessageEvent``）。
        config_default: 该执行器在 ``agent_runner.config`` 中的默认配置模板。
            注意 ``normalize_agent_runner`` 会以本模板为白名单裁剪用户配置，
            模板里没有的字段不会传递给 runner。切换执行器时，WebUI 会把本模板
            深拷贝进 ``agent_runner.config``，因此它同时也是「面板上的初始值」。
        config_metadata: 各配置字段在 WebUI 上的展示元数据，用于在「AI -> Agent
            执行方式」页面渲染该执行器的配置项（内核把它渲染成
            ``<runner_type>_runner`` 分组）。键为 ``config_default`` 里的字段名，
            值可以是描述字符串，或含 ``description`` / ``type`` / ``hint`` /
            ``secret`` 等键的字典（与内核 ``CONFIG_METADATA_3`` 的字段元数据同构，
            ``type`` 缺省时按 ``config_default`` 的同名值推断）。未被本参数声明的
            字段仍会以上述推断结果出现在面板上，因此本参数只影响文案与类型。
        label: 展示名（WebUI 下拉框的兜底文案）。
        description: 描述文本，仅用于日志与 ``/librunner`` 诊断输出。
        reset_kwargs: 传给 ``runner.reset()`` 的额外关键字参数。可以是字典，
            或返回字典的可调用对象（可接受 0 或 1 个位置参数：消息事件）。这些
            参数会覆盖内核默认传入的同名参数（例如 ``provider_config``）。
        owner: 注册方标识。默认从调用栈推断，建议显式传入插件名。
        replace: 当 ``runner_type`` 已被其它 owner 注册时，是否允许抢占。

    Returns:
        注册结果的只读快照，可用其 ``unregister()`` 注销。

    Raises:
        ValueError: ``runner_type`` 非法、是内置类型，或被其它 owner 占用。
        TypeError: ``factory`` / ``config_default`` / ``reset_kwargs`` 类型非法。
    """
    if not isinstance(runner_type, str) or not runner_type.strip():
        raise ValueError("runner_type 必须是非空字符串")
    runner_type = runner_type.strip()
    if not _RUNNER_TYPE_PATTERN.match(runner_type):
        raise ValueError(
            f"runner_type {runner_type!r} 非法：只允许字母、数字、'_'、'.'、'-'"
        )
    if runner_type in BUILTIN_RUNNER_TYPES:
        raise ValueError(f"runner_type {runner_type!r} 是 AstrBot 内置执行器，不允许注入")
    if not callable(factory):
        raise TypeError("factory 必须是可调用对象")
    if config_default is not None and not isinstance(config_default, Mapping):
        raise TypeError("config_default 必须是字典")
    if config_metadata is not None and not isinstance(config_metadata, Mapping):
        raise TypeError("config_metadata 必须是字典")
    if reset_kwargs is not None and not (
        callable(reset_kwargs) or isinstance(reset_kwargs, Mapping)
    ):
        raise TypeError("reset_kwargs 必须是字典或返回字典的可调用对象")

    owner = owner or _caller_owner()
    state = _shared_state()
    existing = state["runners"].get(runner_type)
    if existing is not None and existing["owner"] != owner and not replace:
        raise ValueError(
            f"runner_type {runner_type!r} 已被插件 {existing['owner']!r} 注册；"
            "如需接管请传入 replace=True"
        )
    if existing is not None and existing["owner"] != owner:
        logger.warning(
            "librunner: 插件 %s 覆盖了插件 %s 注册的执行器 %s",
            owner,
            existing["owner"],
            runner_type,
        )

    spec = {
        "runner_type": runner_type,
        "factory": factory,
        "factory_takes_event": _accepts_event(factory),
        "config_default": copy.deepcopy(dict(config_default or {})),
        "config_metadata": _normalize_config_metadata(runner_type, config_metadata),
        "label": label or runner_type,
        "description": description,
        "reset_kwargs": reset_kwargs,
        "reset_kwargs_takes_event": _accepts_event(reset_kwargs),
        "owner": owner,
    }
    state["runners"][runner_type] = spec
    _persist(state)
    _notify()
    logger.info(
        "librunner: 已注册执行器 %s（owner=%s, 配置字段=%d）",
        runner_type,
        owner,
        len(spec["config_default"]),
    )
    return _to_info(spec)


def unregister_runner(runner_type: str, *, owner: str | None = None) -> bool:
    """注销一个已注册的执行器。

    Args:
        runner_type: 执行器类型名。
        owner: 若非 None，则只有该 owner 注册的执行器才会被注销。

    Returns:
        是否确实注销了注册项。
    """
    state = _shared_state()
    spec = state["runners"].get(runner_type)
    if not isinstance(spec, dict):
        return False
    if owner is not None and spec.get("owner") != owner:
        logger.warning(
            "librunner: 拒绝注销执行器 %s：它由 %s 注册，而非 %s",
            runner_type,
            spec.get("owner"),
            owner,
        )
        return False

    state["runners"].pop(runner_type, None)
    _persist(state)
    _notify()
    logger.info("librunner: 已注销执行器 %s（owner=%s）", runner_type, spec.get("owner"))
    return True


def get_runner(runner_type: str | None) -> RunnerInfo | None:
    """查询单个已注册执行器。

    Args:
        runner_type: 执行器类型名。

    Returns:
        只读快照，未注册时返回 None。
    """
    spec = _get_spec(runner_type)
    return _to_info(spec) if spec is not None else None


def list_runners() -> list[RunnerInfo]:
    """列出所有已注册执行器（按类型名排序）。

    Returns:
        只读快照列表。
    """
    specs = _shared_state()["runners"]
    return [_to_info(specs[key]) for key in sorted(specs)]


def has_runner(runner_type: str | None) -> bool:
    """判断某个执行器类型是否已注册。

    Args:
        runner_type: 执行器类型名。

    Returns:
        是否已注册。
    """
    return _get_spec(runner_type) is not None


def registered_types() -> list[str]:
    """返回所有已注册执行器类型名。"""
    return sorted(_shared_state()["runners"])


def known_injected_types() -> set[str]:
    """返回「当前已注册」与「历史上注入过」的类型合集。

    Returns:
        类型名集合。用于判断配置里残留的执行器类型是否曾经由本插件注入。
    """
    state = _shared_state()
    return set(state["runners"]) | set(state.get("persisted") or {})


def get_status() -> dict[str, Any]:
    """返回 librunner 的运行状态，供诊断使用。

    Returns:
        ``{"patched": bool, "patch_error": str | None, "runners": list[str],
        "known_types": list[str]}``
    """
    state = _shared_state()
    return {
        "patched": bool(state.get("patched")),
        "patch_error": state.get("patch_error"),
        "runners": sorted(state["runners"]),
        "known_types": sorted(known_injected_types()),
    }


def _get_spec(runner_type: str | None) -> dict[str, Any] | None:
    """返回内部注册项（含 factory 等运行时对象）。"""
    if not isinstance(runner_type, str):
        return None
    spec = _shared_state()["runners"].get(runner_type)
    return spec if isinstance(spec, dict) else None


def _iter_specs() -> list[dict[str, Any]]:
    """返回全部内部注册项。"""
    specs = _shared_state()["runners"]
    return [specs[key] for key in sorted(specs)]


def _to_info(spec: dict[str, Any]) -> RunnerInfo:
    """把内部注册项转换为只读快照。"""
    return RunnerInfo(
        runner_type=spec["runner_type"],
        owner=spec["owner"],
        label=spec["label"],
        description=spec.get("description", ""),
        config_default=copy.deepcopy(spec.get("config_default") or {}),
        config_metadata=copy.deepcopy(spec.get("config_metadata") or {}),
    )


def _register_listener(listener: Callable[[], None]) -> None:
    """订阅注册表变化事件。"""
    listeners = _shared_state()["listeners"]
    if listener not in listeners:
        listeners.append(listener)


def _unregister_listener(listener: Callable[[], None]) -> None:
    """取消订阅注册表变化事件。"""
    listeners = _shared_state()["listeners"]
    if listener in listeners:
        listeners.remove(listener)


def _set_patch_state(patched: bool, error: str | None) -> None:
    """记录内核补丁的当前状态。"""
    state = _shared_state()
    state["patched"] = bool(patched)
    state["patch_error"] = error
