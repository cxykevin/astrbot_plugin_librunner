"""astrbot_plugin_librunner —— 通用 Agent Runner 注入插件。

本插件提供：

* ``api`` 模块：公共注入接口，其它插件用它注册/注销自定义 Agent 执行器；
* ``patcher`` 模块：内核补丁，把已注册的执行器接进 AstrBot 的第三方执行器
  子阶段（``agent_runner.runner_type``），并让它们出现在 WebUI 的下拉框里；
* ``echo`` 模块：自带的零依赖示例执行器，回复插件配置中的固定文本；
* ``/librunner`` 指令：查看注入状态与已注册的执行器。

WebUI 上的文案（执行器下拉框、「更换执行方式」对话框的标题与说明、执行器配置
分组的描述）本身不属于内核补丁的范畴：它在 dashboard 打包产物与 metadata i18n
转换里，插件没有直接注入通道。这些统一交给**前置插件**
``astrbot_plugin_libi18nject``（见 :mod:`prerequisite`）：本插件只负责把「有哪些
执行器、文案是什么」登记过去。该前置缺失时，``initialize()`` 会尝试自动安装并
就地启用它（**不重启 AstrBot 主进程**）；装不上就停用本插件，并把原因交给 AstrBot
记入「加载失败插件」，在 WebUI 插件页直接可见。

补丁必须在 pipeline 构建之前生效，因此应用时机是 ``initialize()``
（AstrBot 启动顺序：插件加载 -> provider 初始化 -> pipeline 构建）。
插件元数据（名称/作者/版本/依赖的 AstrBot 版本）以 ``metadata.yaml`` 为准，
因此这里不使用已废弃的 ``@register`` 装饰器。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import api, prerequisite
from .echo import DEFAULT_ECHO_REPLY, ECHO_RUNNER_TYPE, EchoAgentRunner
from .patcher import ORPHAN_FALLBACK, ORPHAN_POLICIES, CorePatcher, _build_runner_group
from .prerequisite import Prerequisite, PrerequisiteError

try:  # 前置插件：WebUI 文案注入由 astrbot_plugin_libi18nject 统一负责
    from data.plugins.astrbot_plugin_libi18nject import api as libi18n
except Exception:  # noqa: BLE001 - 未安装时由 prerequisite 负责补装，见 _prepare_prerequisite
    libi18n = None  # type: ignore[assignment]

__version__ = "v1.0.0"


class LibrunnerPlugin(Star):
    """Librunner · Agent Runner 注入接口。

    它为其它插件提供通用的 Agent 执行器注入能力：注入方调用
    ``data.plugins.astrbot_plugin_librunner.api.register_runner`` 注册自己的
    runner，随后即可在 WebUI 的「AI -> Agent 执行方式」中选用（含执行器自己的
    配置项，由注入方通过 ``config_metadata`` 声明）。插件同时自带一个零依赖的
    ``echo`` 执行器，回复文本取面板上的执行器配置，缺省回落到插件配置项
    ``echo_reply``。

    前置插件：WebUI 文案由 ``astrbot_plugin_libi18nject`` 注入。启动时先检查它，
    缺失就自动 ``gh repo clone`` / ``git clone`` 把前置装上并就地启用（**不重启
    AstrBot 主进程**）；装不上则停用本插件，原因由 AstrBot 记入「加载失败插件」，
    在 WebUI 插件页直接可见。

    指令：
    - ``/librunner``：查看内核补丁状态、前置插件状态与已注册的执行器。
    """

    def __init__(self, context: Context, config: Mapping | None = None) -> None:
        """初始化插件。

        Args:
            context: AstrBot 插件上下文。
            config: 插件配置（``_conf_schema.json`` 的实例化结果）。
        """
        super().__init__(context)
        self.plugin_config = config if isinstance(config, Mapping) else {}
        self._patcher: CorePatcher | None = None
        self._prerequisite_instance: Any | None = None
        """就地拉起的前置插件实例：退出时一并释放，避免补丁残留。"""
        self._prerequisite_state = ""
        """最近一次前置检查的结论，用于 ``/librunner`` 展示。"""

    async def initialize(self) -> None:
        """插件实例化后由 AstrBot 自动调用：先确保前置插件，再启用注入接口。

        Raises:
            PrerequisiteError: 前置插件缺失且自动安装失败。AstrBot 会把它记入
                「加载失败插件」，因此失败原因在 WebUI 插件页直接可见，同时本插件
                处于「未启用」状态（即停止工作）。
        """
        if not await self._prepare_prerequisite():
            return
        self._activate_injection()

    def _activate_injection(self) -> None:
        """启用注入接口：注册自带执行器、打内核补丁、登记 WebUI 文案。"""
        self._register_echo_runner()

        policy = self._resolve_orphan_policy()
        self._patcher = CorePatcher(orphan_policy=policy)
        if not self._patcher.apply():
            logger.error(
                "librunner: 内核补丁未能应用，Agent Runner 注入接口当前不可用：%s",
                api.get_status().get("patch_error"),
            )

        api._register_listener(self._refresh_i18n)
        self._refresh_i18n()

    async def _prepare_prerequisite(self) -> bool:
        """确保前置插件就绪。

        缺失时自动安装（``gh repo clone`` → ``git clone`` 回退），装好后**就地**
        启用前置插件，因此不需要重启 AstrBot 主进程；装不上则返回 False（调用方
        抛出 :class:`PrerequisiteError` 停止工作并向前端报告）。

        Returns:
            True 表示可以继续启用注入接口。
        """
        prereq = self._prerequisite()
        enforce = self._bool_config("enforce_prerequisite", True)
        result = await prerequisite.ensure(
            prereq,
            context=self.context,
            auto_install=enforce,
        )
        if result.ready:
            self._prerequisite_state = (
                "已自动安装并就地启用" if result.installed_now else "已安装"
            )
        else:
            self._prerequisite_state = result.message

        if not result.ready:
            if not enforce:
                logger.warning(
                    "librunner: 前置插件未就绪（%s）；配置里未强制要求，继续启用，"
                    "面板文案可能缺词条。",
                    result.message,
                )
                return True
            raise PrerequisiteError(
                f"前置插件未就绪：{result.message}。librunner 的注入接口需要 "
                f"{prereq.describe()}；请检查网络后在内核插件页重载本插件，"
                "或在插件配置里关掉「强制前置插件」。",
            )

        if result.installed_now:
            activation = result.activation
            if activation is not None and activation.instance is not None:
                self._prerequisite_instance = activation.instance
            logger.info(
                "librunner: %s；已就地启用，无需重启 AstrBot 主进程。",
                result.message,
            )
        return True

    def _prerequisite(self) -> Prerequisite:
        """按插件配置构造前置插件描述。"""
        name = self.plugin_config.get("prerequisite_dir_name")
        repo = self.plugin_config.get("prerequisite_repo")
        return Prerequisite(
            name=name.strip()
            if isinstance(name, str) and name.strip()
            else prerequisite.DEFAULT_PREREQUISITE_NAME,
            repo=repo.strip()
            if isinstance(repo, str) and repo.strip()
            else prerequisite.DEFAULT_PREREQUISITE_REPO,
        )

    def _bool_config(self, key: str, default: bool) -> bool:
        """读取布尔配置项（缺失或类型不符时用默认值）。"""
        value = self.plugin_config.get(key, default)
        return value if isinstance(value, bool) else default

    def _libi18n(self) -> Any:
        """返回前置插件的 ``api`` 模块。

        前置可能是本插件刚 clone 下来的（内核还没来得及加载它），所以不能只依赖
        模块导入期的结果：这里未就绪时每次都会重新尝试导入。
        """
        global libi18n
        if libi18n is not None:
            return libi18n
        module = prerequisite.import_plugin_module(
            self._prerequisite().name, module="api"
        )
        if module is not None:
            libi18n = module
        return module

    async def terminate(self) -> None:
        """插件被停用、卸载或重载时由 AstrBot 调用：注销自带执行器并撤销补丁。"""
        api._unregister_listener(self._refresh_i18n)
        api.unregister_runner(ECHO_RUNNER_TYPE, owner=api.PLUGIN_NAME)
        module = self._libi18n()
        if module is not None:
            try:
                module.unregister(owner=api.PLUGIN_NAME)
            except Exception:  # noqa: BLE001 - 文案清理失败不影响退出
                logger.debug("librunner: 注销 WebUI 文案失败", exc_info=True)
        instance = self._prerequisite_instance
        if instance is not None:
            # 前置是我们就地拉起的：退出时把它的补丁一并还原，避免残留。
            self._prerequisite_instance = None
            try:
                await instance.terminate()
            except Exception:  # noqa: BLE001 - 尽力而为
                logger.debug("librunner: 释放就地拉起的前置插件失败", exc_info=True)
        if self._patcher is not None:
            self._patcher.revert()
            self._patcher = None

    def _register_echo_runner(self) -> None:
        """注册自带的 echo 执行器。

        回复文本有两个来源：优先取「AI -> Agent 执行方式」页面上的执行器配置
        （``agent_runner.config.echo_reply``，由本插件注入进面板），未填写时回落到
        插件配置项 ``echo_reply``。后者同时充当面板上的初始值（``config_default``，
        切换执行器时会被写入面板）。两个来源都在每次请求时现取，因此改完保存即刻
        生效，无需重载插件或改动代码。
        """
        api.register_runner(
            ECHO_RUNNER_TYPE,
            factory=EchoAgentRunner,
            config_default={"echo_reply": self._default_echo_reply()},
            config_metadata={
                "echo_reply": {
                    "description": "回复内容",
                    "type": "string",
                    "hint": "机器人对每条消息都回复这段固定文本；留空则回落到 librunner 插件配置中的 echo 回复内容。",
                }
            },
            label="Echo",
            description="librunner 自带示例执行器：回复配置中的固定文本",
            reset_kwargs=self._echo_reset_kwargs,
            owner=api.PLUGIN_NAME,
        )

    def _default_echo_reply(self) -> str:
        """返回面板上的初始回复文本。

        Returns:
            插件配置项 ``echo_reply``；未设置或留空时返回内置兜底文案。
        """
        value = self.plugin_config.get("echo_reply")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return DEFAULT_ECHO_REPLY

    def _echo_reset_kwargs(self) -> dict[str, object]:
        """把插件配置里的回复文本交给 echo 执行器。

        它只是「面板没填时的默认值」：``agent_runner.config.echo_reply`` 存在时
        优先使用面板上的值（见 :func:`echo._resolve_reply`）。

        Returns:
            传给 ``EchoAgentRunner.reset()`` 的额外参数；值为空时由执行器依次
            尝试面板配置与内置兜底文案。
        """
        return {"echo_reply": self.plugin_config.get("echo_reply")}

    def _refresh_i18n(self) -> None:
        """把已注册执行器的 WebUI 文案同步给 libi18nject。

        本插件只负责「有哪些执行器、文案是什么」，具体怎么把文案送进浏览器由
        ``astrbot_plugin_libi18nject`` 负责（它在响应层改写语言包，并保护 metadata
        分组不被转成 i18n key）。未安装该插件时本方法直接返回：功能完全不受影响，
        只是面板上会显示 ``[MISSING: ...]`` 占位。

        本方法同时注册为注册表变化的监听者（见 :meth:`initialize`），因此注入方
        随时注册新执行器都会立刻拿到正确文案。
        """
        module = self._libi18n()
        if module is None:
            return
        try:
            module.register_i18n_entries(self._i18n_entries(), owner=api.PLUGIN_NAME)
            module.register_plain_text_sections(
                self._plain_text_sections, owner=api.PLUGIN_NAME
            )
        except Exception:  # noqa: BLE001 - 文案同步失败不影响执行器功能
            logger.error("librunner: 同步 WebUI 文案失败", exc_info=True)

    def _i18n_entries(self) -> dict[str, dict[str, str]]:
        """构造执行器相关的 WebUI 词条。

        注册项里只有 ``label`` / ``description`` 两个文案来源、不分语言，因此统一
        登记到通配 locale（``"*"``，作为所有语言的兜底）；注入方仍可按语言单独覆盖。

        Returns:
            ``{"*": {"features.config.aiSettings.runners.<type>.title": ...}}``；
            没有已注册执行器时返回空表。
        """
        table: dict[str, str] = {}
        for info in api.list_runners():
            prefix = f"features.config.aiSettings.runners.{info.runner_type}."
            summary = info.description or f"当前由 {info.label} 处理 AI 对话。"
            table[f"{prefix}title"] = info.label
            table[f"{prefix}summary"] = summary
            table[f"{prefix}description"] = summary
        return {"*": table} if table else {}

    def _plain_text_sections(self) -> dict[str, dict[str, object]]:
        """构造「执行器配置分组」的纯文本版本，交给 libi18nject 保护。

        内核会把 metadata 中每个分组的 ``description`` / ``hint`` 换成
        ``ai_group.<section>...`` 形式的 i18n key，而注入的执行器没有对应词条；
        登记为纯文本后，前端 ``translateIfKey()`` 查不到 key 就会原样显示。

        Returns:
            ``{"<runner_type>_runner": <分组元数据>}``。
        """
        sections: dict[str, dict[str, object]] = {}
        for spec in api._iter_specs():
            group = _build_runner_group(spec)
            if isinstance(group, dict):
                sections[f"{spec['runner_type']}_runner"] = group
        return sections

    def _resolve_orphan_policy(self) -> str:
        """读取插件配置中的残留执行器处理策略。

        Returns:
            合法的策略名，配置缺失或非法时返回默认值。
        """
        value = self.plugin_config.get("orphan_runner_policy", ORPHAN_FALLBACK)
        if value in ORPHAN_POLICIES:
            return value
        logger.warning(
            "librunner: 未知的 orphan_runner_policy=%r，使用默认值 %s",
            value,
            ORPHAN_FALLBACK,
        )
        return ORPHAN_FALLBACK

    @filter.command("librunner")
    async def librunner(self, event: AstrMessageEvent):
        """查看 librunner 状态与已注册的 Agent 执行器"""
        yield event.plain_result(self._render_status())

    def _render_status(self) -> str:
        """渲染 ``/librunner`` 的文本输出。"""
        status = api.get_status()
        prereq = self._prerequisite()
        lines = [
            f"librunner {__version__}",
            f"内核补丁: {'已应用' if status['patched'] else '未应用'}",
            f"前置插件 {prereq.name}: {self._prerequisite_state or '未检查'}",
        ]
        if not status["patched"] and status["patch_error"]:
            lines.append(f"失败原因: {status['patch_error']}")

        runners = api.list_runners()
        if runners:
            lines.append(f"已注册执行器（{len(runners)}）:")
            for info in runners:
                detail = f"- {info.runner_type}（owner={info.owner}"
                if info.label and info.label != info.runner_type:
                    detail += f", label={info.label}"
                detail += f", 配置字段 {len(info.config_default)} 项）"
                lines.append(detail)
        else:
            lines.append("已注册执行器: 无（暂无插件调用 register_runner）")

        offline = sorted(api.known_injected_types() - set(status["runners"]))
        if offline:
            lines.append(f"历史注入但当前离线: {', '.join(offline)}")
        return "\n".join(lines)
