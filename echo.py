"""librunner 自带的 echo 执行器。

插件面板里把它选为「执行器」后，机器人会对每条消息回复一段固定文本。文本
优先取「AI -> Agent 执行方式」页面上的执行器配置（``agent_runner.config``
的 ``echo_reply`` 字段），未设置时回落到插件配置项 ``echo_reply``，两者都
留空则使用 :data:`DEFAULT_ECHO_REPLY`。

它对本插件有两重用途：

* 功能上：一个零依赖的「哑执行器」，用来确认 AI 消息链路通不通；
* 教学上：它是注入接口的最小可运行示例 —— 只实现 ``BaseAgentRunner`` 的五个
  方法、不访问任何外部服务，可以照抄它来写自己的 runner，也可以在排查别家
  注入方插件的故障时用它做对照（能通说明 librunner 这一层没问题）。
"""

from __future__ import annotations

import typing as T
from collections.abc import Mapping

from astrbot.api import logger
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.response import AgentResponse, AgentResponseData
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.base import AgentState, BaseAgentRunner
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

#: echo 执行器类型名（即 ``agent_runner.runner_type`` 的取值）。
ECHO_RUNNER_TYPE = "echo"

#: 插件配置未设置 / 留空 ``echo_reply`` 时使用的兜底文案。
DEFAULT_ECHO_REPLY = "这是 librunner 的 echo 执行器，回复内容可以在插件配置里修改。"


def _resolve_reply(echo_reply: object, provider_config: object) -> str:
    """解析要回复的文本。

    Args:
        echo_reply: 插件配置项 ``echo_reply``（由注入方通过 ``reset_kwargs`` 传入），
            用作面板未填写时的默认值。
        provider_config: 内核传入的 ``agent_runner.config``，即面板上配置的执行器
            参数；其中的 ``echo_reply`` 优先级最高。

    Returns:
        非空回复文本，全部候选都不可用时返回 :data:`DEFAULT_ECHO_REPLY`。
    """
    candidates: list[object] = []
    if isinstance(provider_config, Mapping):
        candidates.append(provider_config.get("echo_reply"))
    candidates.append(echo_reply)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return DEFAULT_ECHO_REPLY


class EchoAgentRunner(BaseAgentRunner[AstrAgentContext]):
    """把配置好的文本原样回复，不访问任何外部服务。"""

    def __init__(self) -> None:
        self.req: ProviderRequest | None = None
        """本次请求。"""
        self.run_context: ContextWrapper[AstrAgentContext] | None = None
        """运行时上下文。"""
        self.agent_hooks: BaseAgentRunHooks[AstrAgentContext] | None = None
        """Agent 钩子。"""
        self.reply: str = DEFAULT_ECHO_REPLY
        """本次要回复的文本。"""
        self.final_llm_resp: LLMResponse | None = None
        """最终结果，供内核聚合。"""
        self._state = AgentState.IDLE

    async def reset(
        self,
        request: ProviderRequest | None = None,
        run_context: ContextWrapper[AstrAgentContext] | None = None,
        agent_hooks: BaseAgentRunHooks[AstrAgentContext] | None = None,
        provider_config: Mapping[str, T.Any] | None = None,
        **kwargs: T.Any,
    ) -> None:
        """重置本次请求的状态。

        Args:
            request: 内核构造的 ``ProviderRequest``。
            run_context: 运行时上下文。
            agent_hooks: Agent 钩子。
            provider_config: 内核传入的 ``agent_runner.config``。
            **kwargs: 额外参数，其中 ``echo_reply`` 由本插件注入（插件配置项）。
        """
        self.req = request
        self.run_context = run_context
        self.agent_hooks = agent_hooks
        self.final_llm_resp = None
        self._state = AgentState.IDLE
        self.reply = _resolve_reply(kwargs.get("echo_reply"), provider_config)
        logger.debug(
            "echo 执行器已重置（streaming=%s, 回复长度=%d）",
            bool(kwargs.get("streaming", False)),
            len(self.reply),
        )

    async def step(self) -> T.AsyncGenerator[AgentResponse, None]:
        """单步执行：把回复文本推给内核。

        Yields:
            与内置第三方执行器相同的两种响应：``streaming_delta`` 用于流式输出，
            ``llm_result`` 用于整段输出（内核按配置二选一消费）。
        """
        if self._state in (AgentState.DONE, AgentState.ERROR):
            return

        if self._state is AgentState.IDLE and self.agent_hooks is not None:
            try:
                await self.agent_hooks.on_agent_begin(self.run_context)
            except Exception as exc:  # noqa: BLE001 - 钩子失败不影响回复
                logger.error("echo 执行器的 on_agent_begin 钩子失败: %s", exc, exc_info=True)

        self._transition_state(AgentState.RUNNING)
        chain = MessageChain().message(self.reply)
        yield AgentResponse(
            type="streaming_delta",
            data=AgentResponseData(chain=MessageChain().message(self.reply)),
        )
        yield AgentResponse(type="llm_result", data=AgentResponseData(chain=chain))

        self.final_llm_resp = LLMResponse(role="assistant", result_chain=chain)
        self._transition_state(AgentState.DONE)

    async def step_until_done(
        self,
        max_step: int = 128,
    ) -> T.AsyncGenerator[AgentResponse, None]:
        """一直执行到完成（本执行器一步即可完成）。"""
        while not self.done():
            async for response in self.step():
                yield response

    def done(self) -> bool:
        """本次请求是否已处理完毕。"""
        return self._state in (AgentState.DONE, AgentState.ERROR)

    def get_final_llm_resp(self) -> LLMResponse | None:
        """返回最终结果，供内核的结果聚合器使用。"""
        return self.final_llm_resp
