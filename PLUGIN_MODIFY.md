# PLUGIN_MODIFY.md

> **在 AstrBot v4.29.0 上用 Monkey Patch 接入自定义 Agent Runner 的改造方案**
>
> - 目标读者：维护 `astrbot_plugin_0528`（Alkaid0 中继插件）的开发者
> - 参考实现：`cxykevin/astrbot_plugin_0528`（`alkaid0_agent_runner.py` / `main.py`），针对**老版本** AstrBot 编写
> - 基线代码：AstrBot **v4.29.0**（`master`，HEAD `db4ce0c4b`）
> - 本文所有行号均对应上述基线；AstrBot 迭代很快，**升级后行号必然会漂移，请以符号名定位而非行号**

---

## 0. TL;DR

1. 参考实现选择的**主接缝是对的**：`ThirdPartyAgentSubStage.process` 是当前唯一"官方支撑第三方 runner"的注入点，且插件 `initialize()` 的时机**早于** pipeline 构建，patch 来得及生效。
2. 但它依赖的 **3 个关键符号在 v4.29.0 已经不存在或被改名**：
   - `AGENT_RUNNER_TYPE_KEY`、`self.prov_id` → 已被 `agent_runner.config` 机制取代（架构性变更）
   - schema key `provider_settings.agent_runner_type` → 已改名为 `agent_runner.runner_type`
   - `from third_party import astrbot_config` → 当前该模块不导出此符号
3. 直接把插件装上去的后果是**「patch 成功但静默失效」**：`self.runner_type` 永远不等于 `"alkaid0"`，自定义分支根本不执行；而一旦真把 `runner_type` 设为 `"alkaid0"`，`normalize_agent_runner()` 会在 pipeline 初始化时**直接抛 `ValueError`**，属于启动期崩溃。
4. 结论：**方案 A（留在 third_party 接缝，补齐 4 个 patch 点）**，或更干净的**方案 B（改在 `AgentRequestSubStage` 接缝）**。不建议方案 C（替换本地 `AgentRunner` 别名），因为 `run_agent()` 对 runner 的**私有属性契约**很重。

---

## 1. 背景

`astrbot_plugin_0528` 通过 monkey patch 把一个自定义 `Alkaid0AgentRunner`（继承 `BaseAgentRunner`，走 ACP 协议连接 Alkaid0 服务）注入 AstrBot，作为第三方 Agent 执行器，**不改动 AstrBot 核心代码**。

插件侧的核心文件：

| 文件 | 作用 |
| --- | --- |
| `alkaid0_agent_runner.py` | `Alkaid0AgentRunner(BaseAgentRunner[TContext])`，实现 `reset/step/step_until_done/done/get_final_llm_resp/close` |
| `alkaid0_client.py` | ACP over WebSocket 客户端 |
| `alkaid0_provider.py` | 虚拟 provider（占位） |
| `main.py` | 插件主体：monkey patch + 命令 + Web API + 会话映射持久化 |

**已知前提**：该实现针对老版本 AstrBot，`main.py` / `alkaid0_agent_runner.py` 中存在较多遗留 bug，本次改造目标是**适配当前版本并收紧可靠性**。

---

## 2. 参考实现的依赖符号核对（v4.29.0 实测）

### 2.1 仍然存在且签名兼容 ✅

| 符号 | 用途 | 位置 |
| --- | --- | --- |
| `ThirdPartyAgentSubStage.process(event, provider_wake_prefix)` | 主接缝（async generator） | `astrbot/core/pipeline/process_stage/method/agent_sub_stages/third_party.py:280` |
| `MAIN_AGENT_HOOKS` | Agent hooks 单例 | `astrbot/core/astr_agent_hooks.py:77`，third_party 已 import |
| `ProviderRequest` | 构造请求 | third_party 已 import |
| `run_third_party_agent(runner, stream_to_general=False, custom_error_message=None)` | 消费 runner 输出 | `third_party.py:55` |
| `_RunnerResultAggregator` | 结果聚合（`.add_chunk()` / `.finalize()`） | `third_party.py:87` |
| `_close_runner_if_supported(runner)` | 鸭子类型调 `close()` | `third_party.py:144` |
| `_start_stream_watchdog(*, timeout_sec, is_stream_consumed, close_runner_once)` | 流式看门狗 | `third_party.py:117` |
| `STREAM_CONSUMPTION_CLOSE_TIMEOUT_SEC` | 默认 30s | `third_party.py:41` |
| `THIRD_PARTY_RUNNER_ERROR_EXTRA_KEY` | 错误标记 extra key | `third_party.py:46` |
| `coerce_int_config` | 数值配置强制转换 | `astrbot/core/utils/config_number.py` |
| `AstrAgentContext` / `AgentContextWrapper` | 运行时上下文 | `astrbot/core/astr_agent_context.py` |
| `ResultContentType.{LLM_RESULT, AGENT_RUNNER_ERROR, GENERAL_RESULT, STREAMING_RESULT, STREAMING_FINISH}` | 结果类型枚举 | `astrbot/core/message/message_event_result.py:208` |
| `MessageEventResult.set_async_stream()` / `.set_result_content_type()` | 结果设置 | 同文件 `:260` / `:265` |
| `WakingCheckStage.process` | 保存原始 `message_str` | `astrbot/core/pipeline/waking_check/stage.py:82` |
| `pipeline.context_utils.call_handler` | 手动派发 handler | `astrbot/core/pipeline/context_utils.py:12` |
| `event.plugins_name` / `event.is_at_or_wake_command` | 事件字段 | `astrbot/core/platform/astr_message_event.py:99` / `:55` |
| `ConfigProfileService` / `ProviderConfigService` | 面板 patch 目标 | `astrbot/dashboard/services/config_service.py` |
| `astrbot.core.astrbot_config` | 全局配置单例 | `astrbot/core/__init__.py:32`（⚠️ 见 §7.1） |

### 2.2 已消失 / 已改名 ❌

| 参考实现依赖 | v4.29.0 现状 | 影响 |
| --- | --- | --- |
| `tp.AGENT_RUNNER_TYPE_KEY` | **不存在**（0 命中） | 注册 `["alkaid0"]` 的写法无效 |
| `self.prov_id`（substage 属性） | **不存在** | 分支内访问即 `AttributeError` |
| `from ...third_party import astrbot_config` | **该模块不导出** | `ImportError` |
| schema key `provider_settings.agent_runner_type` | 实为 **`agent_runner.runner_type`** | patch 打空，WebUI 下拉无新增项 |

---

## 3. 三个致命断裂点

### 3.1 架构换代：provider 驱动 → `agent_runner.config` 驱动

**老版本（参考实现假设）**：第三方 runner 通过"选一个 provider 配置"来配置，存在 `runner_type → provider_id 字段名` 的映射表与 substage 上的 `self.prov_id`。

**v4.29.0**（`third_party.py:157-175`）：

```python
class ThirdPartyAgentSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.ctx = ctx
        self.conf = ctx.astrbot_config
        agent_runner = self.conf["agent_runner"]
        self.runner_type = agent_runner["runner_type"]
        self.runner_config = agent_runner["config"]   # ← runner 参数直接内嵌，无 provider_id 层
        settings = ctx.astrbot_config["provider_settings"]
        self.streaming_response: bool = settings["streaming_response"]
        self.unsupported_streaming_strategy: str = settings["unsupported_streaming_strategy"]
        self.stream_consumption_close_timeout_sec: int = coerce_int_config(...)
```

对应的 runner 选择是**硬编码 if/elif**（`third_party.py:336-346`）：

```python
if self.runner_type == "dify":           runner = DifyAgentRunner[AstrAgentContext]()
elif self.runner_type == "coze":         runner = CozeAgentRunner[AstrAgentContext]()
elif self.runner_type == "dashscope":    runner = DashscopeAgentRunner[AstrAgentContext]()
elif self.runner_type == DEERFLOW_PROVIDER_TYPE: runner = DeerFlowAgentRunner[AstrAgentContext]()
else: raise ValueError(f"Unsupported third party agent runner type: {self.runner_type}")
```

> **改造成本**：`provider_config=prov_cfg` 要换成 `provider_config=self.runner_config`；所有 `self.prov_id` / `astrbot_config["provider"]` 查表逻辑要删除。

### 3.2 `normalize_agent_runner()` 是硬闸门

`agent_request.py:79` 无条件调用，且**没有 try/except**：

```python
agent_runner = normalize_agent_runner(self.config.get("agent_runner"))
self.config["agent_runner"] = agent_runner
agent_runner_type = agent_runner["runner_type"]
if agent_runner_type == "local":
    self.agent_sub_stage = InternalAgentSubStage()
else:
    self.agent_sub_stage = ThirdPartyAgentSubStage()
```

而 `astrbot/core/config/agent_runner.py:6,187` 是硬编码常量：

```python
AGENT_RUNNER_TYPES = ("local", "dify", "coze", "dashscope", "deerflow")
...
if runner_type not in AGENT_RUNNER_TYPES:
    raise ValueError(f"Unsupported Agent Runner type: {runner_type}")
```

> **结论**：想让 `runner_type == "alkaid0"` 通过，**必须**在 pipeline 构建之前把 `"alkaid0"` 加进 `AGENT_RUNNER_TYPES`，并给 `AGENT_RUNNER_CONFIG_DEFAULTS` 补一份默认配置（`normalize_agent_runner` 内部会用它做 `_normalize_value` 的模板）。

### 3.3 schema key 改名 + options 硬编码

`astrbot/core/config/default.py` 里（`CONFIG_METADATA_3`，约 `:3289`）：

```python
CONFIG_METADATA_3 = {
    "ai_group": {
        "metadata": {
            "agent_runner": {
                "items": {
                    "provider_settings.enable": {...},
                    "agent_runner.runner_type": {          # ← 注意不是 provider_settings.*
                        "description": "执行器",
                        "type": "string",
                        "options": ["local", "dify", "coze", "dashscope", "deerflow"],
                        "labels": ["内置 Agent", "Dify", "Coze", "阿里云百炼应用", "DeerFlow"],
                        "_special": "agent_runner_type",
                        "runner_defaults": {
                            runner_type: get_agent_runner_config_default(runner_type)
                            for runner_type in ("local", "dify", "coze", "dashscope", "deerflow")
                        },
                    },
                    ...
```

> **改造成本**：patch 目标 key 要从 `provider_settings.agent_runner_type` 改为 `agent_runner.runner_type`，并且需要**同时**处理 `options` / `labels` / `runner_defaults` 三处（`runner_defaults` 是独立硬编码的 dict，不改的话前端切到新类型的表单可能拿不到默认值）。
> `CONFIG_METADATA_2` 里另有同类定义（`default.py` 中约 `:3093`），需一并核对。

---

## 4. 关键时序：为什么 patch 是可行的

`astrbot/core/core_lifecycle.py` 的启动顺序（实测）：

```
:259  self.plugin_manager = PluginManager(self.star_context, self.astrbot_config)
:265  await self.plugin_manager.reload()          # ← 插件扫描/实例化/initialize()：patch 在这里执行
:266  await self.provider_manager.initialize()
:269  await self.kb_manager.initialize()
:272  self.pipeline_scheduler_mapping = await self.load_pipeline_scheduler()
                                                  # ← 这里才 initialize 各 stage
                                                  #   （含 AgentRequestSubStage / ThirdPartyAgentSubStage）
```

**插件先加载、pipeline 后构建** → 在插件 `initialize()` 里修改：

- 模块级常量（`AGENT_RUNNER_TYPES` / `AGENT_RUNNER_CONFIG_DEFAULTS`）
- 模块级 dict（`CONFIG_METADATA_2` / `CONFIG_METADATA_3`）
- 类方法（`ThirdPartyAgentSubStage.process`）

**都能影响随后构建的 pipeline**。这是整个方案成立的前提。

> ⚠️ 但**配置读取是在 `AgentRequestSubStage.initialize()` 里做校验的**，所以第 (1) 步（放行类型）必须在此之前完成 —— 即必须在插件 `initialize()` 内，而不能拖到第一条消息。

---

## 5. 方案选型

### 方案 A：留在 `ThirdPartyAgentSubStage` 接缝（推荐，贴近现有实现）

**做法**：补齐 4 个 patch 点，把配置来源从 provider 改为 `self.runner_config`。

- ✅ 改动最小，能复用参考实现 ~80% 的代码
- ✅ 复用核心的 `run_third_party_agent` / aggregator / watchdog，流式与非流式结果语义与内置第三方 runner 完全一致
- ⚠️ 与 `third_party.py` 内部细节耦合较深

### 方案 B：改在 `AgentRequestSubStage` 接缝（最干净）

`agent_request.py:88-102` 的 `process()` 只有 6 行，全部工作是委派：

```python
async for resp in self.agent_sub_stage.process(event, self.prov_wake_prefix):
    yield resp
```

**做法**：patch `AgentRequestSubStage.process`（或 `initialize` 里的 sub-stage 选择），判定 `runner_type == "alkaid0"` 时走自己的完整流程。

- ✅ 不必碰 `third_party.py` 的内部实现细节
- ⚠️ 需要自己实现流式/非流式两套 `ResultContentType` 设置（可从 `_handle_streaming_response` / `_handle_non_streaming_response` 移植）
- ⚠️ 仍必须先做方案 A 的第 (1)(3) 步放行配置校验

### 方案 C：替换本地 runner 别名（不推荐）

```python
import astrbot.core.astr_main_agent as m
m.AgentRunner = Alkaid0AgentRunner    # astr_main_agent.py:21 是模块级名字，替换即生效
```

看似优雅，实则**隐性契约极重**：

1. `build_main_agent()` 调用 `reset()` 时传的是**本地 runner 的参数集**（`provider=` / `tool_executor=` / `llm_compress_*` / `truncate_turns=` / `enforce_max_turns=` / `tool_schema_mode=` / `fallback_providers=` / `request_max_retries=` …，见 `astr_main_agent.py:1980-2017`），与第三方 runner 的 `reset(request=, run_context=, agent_hooks=, provider_config=)` 签名**不同**。
2. `run_agent()`（`astr_agent_run_util.py:115`）会直接读写这些**私有属性**：
   `_step_budget_max` / `_step_budget_used` / `_step_budget_notified`、`req`、`streaming`、`run_context.context.event`，并调用 `request_stop()`、`done()`。
3. `internal.py:328` 还会 `register_active_runner(event.unified_msg_origin, agent_runner)`，`follow_up.py` 的 `_ACTIVE_AGENT_RUNNERS` 也会用到它。

**结论**：等于要重写一遍 `ToolLoopAgentRunner` 的外壳，**不建议**。

### 方案 D：不上 runner，用 `@filter.on_llm_request` 钩子（保底）

用官方钩子拦截请求 → 转发外部服务 → `event.set_result(...)` + `event.stop_event()`。

- ✅ 完全不碰核心，升级最不容易碎
- ❌ 失去 runner 语义（流式、中断、`register_active_runner`、follow-up）
- 注：third_party 分支内部本身也会触发 `OnLLMRequestEvent` 钩子（`third_party.py:179`）

### 推荐

**方案 A 为主，方案 D 作为降级兜底。** 若后续想降低耦合，再迁到方案 B。

---

## 6. 方案 A 落地清单（代码骨架）

> ⚠️ 以下为**骨架示意**，未在本仓库运行验证；请按实际符号逐步核对后再启用。

### 6.1 放行配置校验（必须最先执行）

```python
import astrbot.core.config.agent_runner as ar

# 1) 放行 runner 类型
if "alkaid0" not in ar.AGENT_RUNNER_TYPES:
    ar.AGENT_RUNNER_TYPES = (*ar.AGENT_RUNNER_TYPES, "alkaid0")

# 2) 提供默认配置模板（normalize_agent_runner 内部用它补全字段）
ar.AGENT_RUNNER_CONFIG_DEFAULTS.setdefault("alkaid0", {
    "alkaid0_host": "127.0.0.1",
    "alkaid0_port": 7433,
    "alkaid0_key": "",
    "alkaid0_key_path": "/etc/alkaid0/config.json",
    "alkaid0_model": "",
    "alkaid0_show_reasoning": True,
    "default_cwd": "/root",
    "alkaid0_keepalive": 3600,
    "custom_extra_body": {},
})
```

说明：`agent_request.py` 虽然 `from ... import normalize_agent_runner` 拿到了函数对象，但该函数**内部**读取的是同模块的 globals，因此只需修改 `astrbot.core.config.agent_runner` 的模块属性即可生效。

### 6.2 主接缝 patch：包住 `ThirdPartyAgentSubStage.process`

结构保持不变（`self.runner_type != "alkaid0"` 时透传原始逻辑），仅替换 Alkaid0 分支内的 3 处：

```python
import astrbot.core.pipeline.process_stage.method.agent_sub_stages.third_party as tp

_original_process = tp.ThirdPartyAgentSubStage.process

@wraps(_original_process)
async def patched_process(self, event, provider_wake_prefix):
    if self.runner_type != "alkaid0":
        async for _ in _original_process(self, event, provider_wake_prefix):
            yield _
        return

    # ❌ 删除：self.prov_id / astrbot_config["provider"] 查表
    # ✅ 改为：直接使用 substage 上的 runner_config
    prov_cfg = self.runner_config or {}

    # ... 命令拦截、ProviderRequest 构建、并发锁、共享连接等逻辑保持 ...

    await runner.reset(
        request=req,
        run_context=AgentContextWrapper(context=astr_agent_ctx, tool_call_timeout=120),
        agent_hooks=MAIN_AGENT_HOOKS,
        provider_config=prov_cfg,          # ← 语义变化：现在是 agent_runner.config 的内容
        streaming=streaming_response,
        existing_session_id=existing_session_id,
        shared_client=shared_client,       # 自定义 kwargs，由 runner 自行消费
    )
    # ... 后续流式/非流式处理保持 ...

tp.ThirdPartyAgentSubStage.process = patched_process
```

> `reset()` 的 `**kwargs` 是自由通道，核心只传 `provider_config` / `streaming`，其余自定义参数（`shared_client` / `existing_session_id`）不会被核心覆盖，可继续使用。

### 6.3 WebUI schema 注入

```python
from astrbot.core.config.default import CONFIG_METADATA_2, CONFIG_METADATA_3
from astrbot.core.config.agent_runner import get_agent_runner_config_default

def _add_runner_option(items: dict, key: str) -> None:
    """向 agent_runner.runner_type 的 options/labels/runner_defaults 注入 alkaid0。"""
    art = items.get(key)
    if not isinstance(art, dict):
        return
    if "alkaid0" not in art.get("options", []):
        art.setdefault("options", []).append("alkaid0")
        art.setdefault("labels", []).append("Alkaid0")
    art.setdefault("runner_defaults", {}).setdefault(
        "alkaid0", get_agent_runner_config_default("alkaid0")
    )

# CONFIG_METADATA_3["ai_group"]["metadata"]["agent_runner"]["items"]["agent_runner.runner_type"]
# CONFIG_METADATA_2 中同类定义需按实际结构定位（约 default.py:3093）
```

**注意**：v4.29.0 的 key 是 **`agent_runner.runner_type`**，不是老版本的 `provider_settings.agent_runner_type`；`options` / `labels` / `runner_defaults` 是**三处独立的硬编码**，漏改任意一处都可能导致前端行为异常。

### 6.4 patch 幂等与恢复（参考实现缺失，建议补上）

```python
_PATCHED_FLAG = "_alkaid0_patched"

def _apply_patch_once():
    if getattr(tp.ThirdPartyAgentSubStage, _PATCHED_FLAG, False):
        return
    setattr(tp.ThirdPartyAgentSubStage, _PATCHED_FLAG, True)
    # ...
```

原因：插件**每次重载都会再跑一次 `initialize()`**，`@wraps` 层层叠加会让 process 调用链不断变长，最终导致性能下降与调试困难。同时在 `terminate()` 中恢复原方法。

### 6.5 执行器配置分组（后续扩展，已实测）

§6.3 只解决了「让类型出现在下拉框」。新类型还需要在 AI 页面上有自己的配置项，
否则用户切过去之后看到的是一片空白。核对 v4.29.0 前端源码后确认：

- 面板渲染第三方执行器配置区时读的是 `ai_group.metadata["<runner_type>_runner"]`
  （`dashboard/src/components/config/AiConfigPanel.vue` 的 `thirdPartyRunnerGroup`），
  结构与内置 `dify_runner` / `coze_runner` 完全同构：`description` + `type: "object"`
  + `condition`（`{"provider_settings.enable": true, "agent_runner.runner_type": <type>}`）
  + `items`（键为 `agent_runner.config.<field>`）；
- 字段文案走 `useConfigTextResolver().translateIfKey()`：能在
  `features.config-metadata` 翻译表里查到才翻译，**查不到就原样显示** —— 纯文本是
  安全的；
- **但后端返回 metadata 前会先做一次 key 转换**：
  `dashboard/services/config_service.py` 的 `get_profile` / `get_profile_schema` 等
  都会调用 `ConfigMetadataI18n.convert_to_i18n_keys()`，把每个分组与字段的
  `description` / `hint` / `labels` / `name` **无条件**替换成
  `ai_group.<section>.<field>.description` 形式的 key（内核自带分组的词条写在
  dashboard 静态语言文件里，所以显示正常）。注入的分组没有这些词条，于是面板显示
  `[MISSING: ai_group.echo_runner.description]` —— 实测确认过这一点。
  这一条现在由姊妹插件 `astrbot_plugin_libi18nject` 兜住：librunner 把注入的执行器
  分组登记过去（`main._plain_text_sections()`），该插件包装 `convert_to_i18n_keys`
  时把登记的 section 换回纯文本（它自己的 `metadata_i18n.py`，可随 `revert()` 完整
  还原）；librunner 侧不再打这个补丁。
- 「更换执行方式」对话框里每个选项的标题/说明是硬编码 key
  `aiSettings.runners.<type>.title` / `.description` / `.summary`（模块
  `features.config`，静态打包进 dashboard）。前端只有 `translateIfKey()` 一条取值
  路径，后端也只有平台配置页会 `mergeDynamicTranslations(...)`，因此**没法从
  metadata 通道补词条**：要么改内核语言文件并重新构建 `data/dist`，要么像
  `astrbot_plugin_libi18nject` 那样在响应层改写语言包 —— 它在静态资源送出前把
  librunner 登记的 `features.config.aiSettings.runners.<type>.*` 词条写进对应
  locale chunk，装上后注入类型不再显示
  `[MISSING: features.config.aiSettings.runners.<type>.title]`。

librunner 的实现对应 `patcher._sync_runner_groups()` / `_build_runner_group()`：
字段来源是注册项的 `config_default`（并充当 `normalize_agent_runner` 的白名单），
文案/类型来自 `config_metadata`（缺省时按默认值推断类型、以字段名作描述）；分组随
注册表变化实时刷新，注销或 `revert()` 时回收，并用 `_is_injected_group()` 区分
「本插件注入的分组」与「内核自带分组」，避免热重载把上一次注入的内容当成原值残留。

因此配置数据链路是：面板写 `agent_runner.config` → 内核以
`runner.reset(provider_config=self.runner_config, ...)` 传给 runner
（`third_party.py:382-389`）。注入方只要读 `provider_config` 即可拿到面板配置，
不需要 librunner 额外搬运。

---

## 7. 坑与风险

### 7.1 `astrbot.core.astrbot_config` 是**默认配置**单例

`core/__init__.py:32` 的 `astrbot_config = AstrBotConfig()` 在**多配置文件（profile / umo 路由）**场景下未必是当前会话使用的配置。

**建议**：改为从插件上下文取会话配置，例如
`ctx.plugin_manager.context.get_config(umo=event.unified_msg_origin)`。

### 7.2 patch 的幂等性

见 §6.4。参考实现在 `initialize()` 里无条件 patch，且（在已阅读的部分中）未见 `terminate()` 恢复逻辑。

### 7.3 `close()` 不在基类契约里

`BaseAgentRunner`（`astrbot/core/agent/runners/base.py:22`）只定义了
`reset` / `step` / `step_until_done` / `done` / `get_final_llm_resp` **5 个抽象方法**。

第三方路径通过 `getattr(runner, "close", None)` **鸭子探测**来清理资源（`third_party.py:144`）；
而**本地路径（方案 C）不会调用 `close()`**，需要自行管理生命周期。

### 7.4 `step_until_done(max_step)` 的语义

`run_third_party_agent` 固定以 `step_until_done(max_step=128)` 调用（`third_party.py:71`），
参考实现内部默认 30 且忽略该参数。不影响正确性，但语义与核心预期不一致。

### 7.5 wake prefix 判断不一致

核心使用 `astrbot.core.astr_main_agent._matches_provider_wake_prefix`（`third_party.py:45` 导入），
参考实现用朴素的 `startswith`。空 prefix、lark 平台 `lstrip` 等边界行为可能不同，建议改为复用核心函数。

### 7.6 与核心版本强耦合

本方案依赖：模块级常量、类方法、config schema 结构、`run_third_party_agent` 的产出协议。
AstrBot 每次小版本迭代都可能改动其中任意一项 —— 需要**把版本号写进插件元数据并做启动期版本断言**，
至少在 patch 失败时显式报错，而不是静默失效。

### 7.7 `validate_config` 是否接受新 options

`normalize_agent_runner` 放行后，配置仍需通过 `validate_config(post_config, CONFIG_METADATA_2, ...)`
（`dashboard/services/config_service.py` 中 `:576` / `:693` 两处调用）。
注入 `options` 是否足够让校验通过，**尚未验证**（见 §9）。

---

## 8. 落地顺序与验证

建议分 4 步走，每步都可独立验证：

| 步骤 | 内容 | 验证方式 |
| --- | --- | --- |
| 1 | 只做 §6.1（放行类型）+ §6.3（schema 注入）+ §6.5（配置分组注入） | 启动 AstrBot，确认**无 ValueError**；WebUI 下拉出现 "Alkaid0"；切过去后 AI 页面出现该执行器的配置项且能保存（WebUI 文案搭配 `astrbot_plugin_libi18nject` 即可补齐，见 §6.5） |
| 2 | 只做 §6.2 的**透传分支**（patch 后日志打印 `self.runner_type` / `self.runner_config`） | 发送一条消息，确认 patch 生效且能读到配置；确认非 alkaid0 时不破坏原有 runner |
| 3 | 接入 Alkaid0 分支（换 `provider_config=self.runner_config`，删 `prov_id` 逻辑），先只跑非流式 | 端到端收到一条回复 |
| 4 | 补流式、取消、watchdog、幂等与恢复 | 流式输出正常；`/approve`、取消、并发同会话场景正常 |

**回归底线**：把 `agent_runner.runner_type` 切回 `local` 与 `dify`（若已配置），确认内置路径行为完全不变 —— 这是 patch 类方案必须守住的红线。

---

## 9. 尚未验证的事项（重要）

本文档的事实来源为：**AstrBot v4.29.0 工作区代码静态阅读** + **`cxykevin/astrbot_plugin_0528` 的 `alkaid0_agent_runner.py` 全文与 `main.py` 前约 1550 行（GitHub raw）**。

以下内容**未经验证**，落地前请自行确认：

1. `main.py` 有 88.9 KB，raw 拉取在 64 KiB 处被截断 —— **后半部分（命令定义、`terminate()`、可能存在的 unpatch 逻辑）未阅读**。"参考实现没有 terminate 恢复"这一判断仅限已读部分。
2. `AGENT_RUNNER_TYPE_KEY` / `self.prov_id` 属于老版本"provider 驱动"机制 —— 这是基于参考实现用法 + 当前版本缺失所做的**推断**，未 checkout 老版本 tag 做正面对比。
3. `CONFIG_METADATA_2` 中 agent_runner 定义的**精确路径**未逐行确认（仅确认 `CONFIG_METADATA_3` 的结构）。
4. `validate_config` 对新增 runner 类型的接受程度（§7.7）。
5. ~~前端（Dashboard）对新 `options` / `labels` / `runner_defaults` 的渲染行为。~~
   已按源码核对（见 §6.5）：`labels` 是**数组则直接显示**、是字符串才按 i18n key 解析，
   因此注入纯文本 `label` 没问题；字段 `description` / `hint` 走 `translateIfKey()`
   （查不到 key 就原样显示）；只有「更换执行方式」对话框的标题/说明是硬编码的静态
   i18n key，未安装 `astrbot_plugin_libi18nject` 时注入类型会显示 `[MISSING: ...]`，
   装上它由响应层改写语言包补齐（§6.5）。仍未做的只有**真实浏览器端点击验证**。
   补充：后端在返回 metadata 前还会调用 `ConfigMetadataI18n.convert_to_i18n_keys()`，
   把分组/字段的文案**无条件**换成 `ai_group.<section>...` 形式的 key —— 这一条
   已交由 `astrbot_plugin_libi18nject` 的 metadata 补丁兜住（§6.5），有自测覆盖。
6. 所有提及的符号仅核对了**存在性与签名**，未做运行时验证；文中代码骨架**未执行过**。

---

## 附录：相关代码位置索引（v4.29.0）

| 主题 | 位置 |
| --- | --- |
| Runner 抽象基类 | `astrbot/core/agent/runners/base.py:22` |
| 本地 runner 实现 | `astrbot/core/agent/runners/tool_loop_agent_runner.py` |
| 第三方 runner 子阶段 | `astrbot/core/pipeline/process_stage/method/agent_sub_stages/third_party.py` |
| 子阶段选择（local vs 第三方） | `astrbot/core/pipeline/process_stage/method/agent_request.py:79-86` |
| runner 配置常量与归一化 | `astrbot/core/config/agent_runner.py:6,9,172,187` |
| 本地 agent 构建 | `astrbot/core/astr_main_agent.py:238,1712,1871,2015` |
| 本地 runner 驱动器 | `astrbot/core/astr_agent_run_util.py:23,115` |
| config schema（runner 类型选项） | `astrbot/core/config/default.py:3093,3289` |
| 配置元数据文案 → i18n key 转换 | `astrbot/core/config/i18n_utils.py`（`ConfigMetadataI18n.convert_to_i18n_keys`） |
| WebUI 文案注入（语言包词条 + 纯文本分组保护） | `data/plugins/astrbot_plugin_libi18nject`（姊妹插件，librunner 只登记内容，不改内核文件与 `data/dist`） |
| 启动顺序（插件早于 pipeline） | `astrbot/core/core_lifecycle.py:259,265,272` |
| 结果类型枚举 | `astrbot/core/message/message_event_result.py:208` |
| 全局配置单例 | `astrbot/core/__init__.py:32` |

---

*本文档由代码静态分析生成，用于指导 `astrbot_plugin_0528` 的版本适配改造；不含可直接投入生产的已验证代码。*
