# astrbot_plugin_librunner

> 通用 **Agent Runner 注入接口**：让 AstrBot 插件把自己的 Agent 执行器接进
> `agent_runner.runner_type`，出现在 WebUI 的「AI -> Agent 执行方式 -> 执行器」
> 下拉框里，**无需 fork / 修改 AstrBot 核心代码**。

本插件提供「公共接口 + 内核补丁」这一层胶水，并自带一个可用的示例执行器：

- 🧩 其它插件调用 `register_runner()` 注册自定义 runner（`BaseAgentRunner` 实现）；
- 🚀 用户在下拉框里选择该执行器，消息就会交给注入的 runner 处理；
- 🐣 自带一个零依赖的 `echo` 执行器（回复内容取 AI 页面上的执行器配置，缺省回落到
  插件配置 `echo_reply`），既能直接用来验证消息链路，也是注入接口的最小示例；
- 🔁 复用 AstrBot 内核的流式输出、看门狗、错误聚合与资源清理逻辑；
- 🧯 注入方插件被禁用/卸载后，配置里残留的执行器类型会优雅降级，而不是让 AstrBot
  在构建 pipeline 时报 `ValueError`。

> [!IMPORTANT]
> 本插件基于 **AstrBot v4.29.0** 的第三方执行器子阶段实现，通过 monkey patch
> 接入内核（`metadata.yaml` 已声明 `astrbot_version: ">=4.29.0"`）。AstrBot 迭代
> 很快，升级 AstrBot 后请先用 `/librunner` 确认补丁是否仍然生效。

---

## 目录

- [安装](#安装)
- [前置插件](#前置插件)
- [快速开始（注入方插件）](#快速开始注入方插件)
- [自带的 echo 执行器](#自带的-echo-执行器)
- [API 参考](#api-参考)
- [工作原理](#工作原理)
- [WebUI 文案](#webui-文案是怎么补上的)
- [配置](#配置)
- [已知限制与风险](#已知限制与风险)
- [故障排查](#故障排查)
- [开发者自测](#开发者自测)

---

## 安装

把插件目录放入 AstrBot 的 `data/plugins/` 下，重启 AstrBot（或在 WebUI 的插件页
重载）。启动日志里会出现：

```
librunner: 已注册执行器 echo（owner=astrbot_plugin_librunner, 配置字段=1）
librunner: 内核补丁已应用（当前已注册执行器: ['echo']）
```

在聊天里发送 `/librunner` 可以随时查看状态：

```
librunner v1.0.0
内核补丁: 已应用
前置插件 astrbot_plugin_libi18nject: 已安装
已注册执行器（1）:
- echo（owner=astrbot_plugin_librunner, label=Echo, 配置字段 1 项）
```

---

## 前置插件

librunner 的 WebUI 文案由 **`astrbot_plugin_libi18nject`** 注入（原理见
[WebUI 文案](#webui-文案是怎么补上的)），所以它是本插件的前置插件。AstrBot 内核
没有插件级依赖声明（`metadata.yaml` 只支持 pip `requirements.txt`），因此本插件
自己做了一套最小可用的前置管理：

| 阶段 | 行为 |
| --- | --- |
| 启动检查 | 看 `data/plugins/astrbot_plugin_libi18nject/metadata.yaml` 是否存在（只看文件，不要求内核已加载它） |
| 自动安装 | 缺失时先 `gh repo clone cxykevin/astrbot_plugin_libi18nject`（复用你已登录的 gh），失败回退 `git clone https://github.com/cxykevin/astrbot_plugin_libi18nject.git`；每条命令最长等 60 秒 |
| 就地启用 | 装好后在同一个 `Context` 上实例化前置插件的 `Star` 类并调用 `initialize()` —— **不重启 AstrBot 主进程**，前置的补丁与登记立即生效 |
| 继续启用 | 前置就绪后本插件照常注册执行器、打内核补丁、登记文案 |
| 失败上报 | 装不上则**停止工作**（不注册执行器、不打补丁）并抛出 `PrerequisiteError`；AstrBot 会把原因记入插件页的「加载失败插件」，前端直接可见 |

几个要点：

- 自动安装**每个进程只尝试一次**，结果记录在
  `data/plugin_data/astrbot_plugin_librunner/prerequisite.json`，反复重载也不重复克隆；
- 换源（内网 / 镜像）改配置里的「前置插件来源」即可；
- 不想被前置挡住，把「强制前置插件」关掉：没有前置也照常启用，只是面板上会有
  `[MISSING: ...]` 占位；
- `terminate()` 时本插件会把自己就地拉起的前置实例一并 `terminate()`，不留补丁。

---

## 快速开始（注入方插件）

注入方插件只需要三步：实现 runner、注册、注销。

```python
from astrbot.api.star import Context, Star
from astrbot.core.agent.runners.base import BaseAgentRunner
from astrbot.core.astr_agent_context import AstrAgentContext

from data.plugins.astrbot_plugin_librunner.api import (
    register_runner,
    unregister_runner,
)

RUNNER_TYPE = "myagent"


class MyAgentRunner(BaseAgentRunner[AstrAgentContext]):
    """按 BaseAgentRunner 契约实现 reset / step_until_done / done /
    get_final_llm_resp，可选实现 close。"""

    async def reset(self, request, run_context, agent_hooks, provider_config=None, **kwargs):
        ...

    async def step_until_done(self, max_step: int = 128):
        ...

    def done(self) -> bool:
        ...

    def get_final_llm_resp(self):
        ...

    async def close(self) -> None:  # 可选
        ...


class MyAgentPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.plugin_config = config or {}

    async def initialize(self):
        register_runner(
            RUNNER_TYPE,
            # factory 可接受 0 个参数，或 1 个位置参数（当前消息事件）
            factory=lambda: MyAgentRunner[AstrAgentContext](),
            config_default={"api_base": "http://127.0.0.1:8080", "timeout": 60},
            # 可选：声明这些字段在 AI 页面上的文案/类型（未声明的字段按默认值推断）
            config_metadata={
                "api_base": "服务地址",
                "timeout": {"description": "超时时间（秒）", "type": "int"},
            },
            label="MyAgent",
            description="示例执行器",
            # 追加/覆盖传给 runner.reset() 的关键字参数，例如把插件自身配置注入进去
            reset_kwargs=lambda event: {"settings": self.plugin_config},
            owner="astrbot_plugin_myagent",
        )

    async def terminate(self):
        unregister_runner(RUNNER_TYPE, owner="astrbot_plugin_myagent")
```

注册完成后，在 WebUI 的「AI -> Agent 执行方式」里把执行器切到 `myagent`
（下拉框里显示的是 `label` / 类型名；内置执行器的中文标签来自前端静态文案，
注入的选项用的是注册时提供的文案），即可开始对话。

> 想先确认链路通不通？可以直接换成插件自带的 `echo` 执行器试一条消息，
> 见[下一节](#自带的-echo-执行器)。

---

## 自带的 echo 执行器

插件启动时会自动注册一个 `echo` 执行器：把执行器切成 `echo` 后，机器人对每条
消息都回复一段固定文本。它既是一个零依赖的「哑执行器」（用来确认 AI 消息链路），
也是排查注入方插件故障时的对照组（`echo` 能通，说明 librunner 这一层没问题）。

1. 在「AI -> Agent 执行方式 -> 执行器」里选择 `echo` 并保存 —— 页面上会出现
   `echo` 自己的配置区，里面有 **回复内容**（`agent_runner.config.echo_reply`），
   默认值取自插件配置项 `echo_reply`；
2. 想要别的文本，直接改这一项（或先改插件配置页的 `echo_reply` 再切换执行器）；
3. 随便发一条消息，就会收到那段文本。

回复文本的取值顺序是：**AI 页面上的执行器配置 → 插件配置项 `echo_reply` →
内置兜底文案**。两个来源都在每次请求时现取，改完保存即刻生效。

它也是写自定义 runner 的最佳模板：代码只有 100 多行，只实现 `BaseAgentRunner`
的 `reset` / `step` / `step_until_done` / `done` / `get_final_llm_resp`，不访问
任何外部服务，见 [`echo.py`](./echo.py)。

> 和内核内置的执行器一样：切换执行方式时，面板会用 `config_default` 重写
> `agent_runner.config`（对 `echo` 就是插件配置里那份文本），之后以面板上的值为准。

---

## API 参考

```python
from data.plugins.astrbot_plugin_librunner.api import ...
```

| 名称 | 说明 |
| --- | --- |
| `register_runner(runner_type, factory, *, config_default=None, config_metadata=None, label=None, description="", reset_kwargs=None, owner=None, replace=False) -> RunnerInfo` | 注册执行器 |
| `unregister_runner(runner_type, *, owner=None) -> bool` | 注销执行器 |
| `get_runner(runner_type) -> RunnerInfo \| None` | 查询单个执行器快照 |
| `list_runners() -> list[RunnerInfo]` | 列出全部执行器快照 |
| `has_runner(runner_type) -> bool` | 是否已注册 |
| `registered_types() -> list[str]` | 全部已注册类型名 |
| `known_injected_types() -> set[str]` | 已注册 + 历史上注入过的类型（含落盘记录） |
| `get_status() -> dict` | `{"patched", "patch_error", "runners", "known_types"}` |

### 参数语义

| 参数 | 语义 |
| --- | --- |
| `runner_type` | 执行器类型名，即 `agent_runner.runner_type` 的取值。仅允许字母/数字/`_`/`.`/`-`，且不能是内置类型（`local`/`dify`/`coze`/`dashscope`/`deerflow`） |
| `factory` | 返回 runner 实例的可调用对象。接受 0 个参数，或 1 个位置参数（`AstrMessageEvent`）。返回值需具备 `reset` / `step_until_done` / `done` / `get_final_llm_resp`，可选 `close` |
| `config_default` | 该执行器在 `agent_runner.config` 中的默认配置模板。**它会同时充当白名单**：AstrBot 的 `normalize_agent_runner()` 只保留模板里出现过的字段，未声明的字段不会传给 runner。切换执行器时，面板会把这份模板深拷贝进 `agent_runner.config`，因此它同时是「面板上的初始值」 |
| `config_metadata` | 各配置字段在 WebUI 上的展示元数据：键为字段名，值可以是描述字符串，或含 `description` / `type` / `hint` / `secret` 等键的字典（与内核 `CONFIG_METADATA_3` 的字段元数据同构）。`type` 缺省时按 `config_default` 的同名值推断（bool/int/float/string/dict/list）。未声明的字段也会出现在面板上，只是描述退化为字段名 |
| `label` | 展示名，用于 WebUI 下拉框的兜底文案、执行器配置区的标题与 `/librunner` 输出 |
| `reset_kwargs` | 追加/覆盖传给 `runner.reset()` 的关键字参数。可以是字典，或返回字典的可调用对象（0 或 1 个位置参数：事件）。同名参数会覆盖内核默认值（例如 `provider_config`） |
| `owner` | 注册方标识，默认从调用栈推断，建议显式传插件名 |
| `replace` | `runner_type` 已被其它 `owner` 注册时是否允许抢占（默认不允许） |

### 生命周期建议

- **注册**：在自己的 `initialize()` 中调用。librunner 的补丁在启动时早于 pipeline
  构建，注册在同一阶段完成即可；即使注入方插件比 librunner 先加载，注册也会被
  保留并在 librunner 就绪后自动生效。
- **注销**：在自己的 `terminate()` 中调用。
- **重载**：注册信息存放在进程级共享状态里，librunner 被热重载/更新后不会丢失。
- 同一个 owner 重复注册同一个 `runner_type` 视为幂等覆盖。

---

## 工作原理

librunner 在插件 `initialize()` 阶段（**早于 pipeline 构建**）打 4 个补丁：

| # | 补丁点 | 目的 |
| --- | --- | --- |
| 1 | `astrbot.core.config.agent_runner.AGENT_RUNNER_TYPES` / `AGENT_RUNNER_CONFIG_DEFAULTS` | 放行新类型并提供配置模板，否则 `normalize_agent_runner()` 会在 pipeline 初始化时抛 `ValueError` |
| 2 | `CONFIG_METADATA_3` 中的 `agent_runner.runner_type`（`options` / `labels` / `runner_defaults`）与 `ai_group.metadata.<type>_runner` 配置分组 | 让新类型出现在 WebUI 下拉框，并在切换类型时带上默认配置；同时让该执行器在 AI 页面上拥有自己的配置项（字段来自 `config_default`，文案来自 `config_metadata`） |
| 3 | `normalize_agent_runner()` | 残留（注入方已下线）类型按 `orphan_runner_policy` 优雅降级 |
| 4 | `ThirdPartyAgentSubStage.process` | 分发点：已注册类型交给注入方的 runner |

WebUI 上的文案（下拉框、「更换执行方式」对话框、配置分组标题）不属于内核补丁的
范畴，统一交给可选的姊妹插件 `astrbot_plugin_libi18nject` 处理，见
[WebUI 文案](#webui-文案是怎么补上的)。librunner 只负责把「有哪些执行器、文案是
什么」登记过去（`main._refresh_i18n`），没装它时功能完全正常，只是面板上会显示
`[MISSING: ...]` 占位。

第 4 点是关键。AstrBot 用一组字面量 `if/elif` 选择执行器（`dify` / `coze` /
`dashscope` / `deerflow`），且没有对外暴露 runner 构造钩子。librunner 的做法是
**不复制内核流程，只替换构造结果**：

1. 未注册的类型 → 原样透传内核实现（内置执行器行为 100% 不变）；
2. 已注册的类型 → 复制一份子阶段实例，仅在副本上把 `runner_type` 换成
   `_RunnerTypeAlias`（它与真实类型相等，也与占位分支 `deerflow` 相等），并把
   `ContextVar` 置为「本次请求要构造注入方的 runner」，然后交给内核原始实现。

于是内核会照常走 deerflow 分支的完整流程（构建 `ProviderRequest`、媒体处理、
`OnLLMRequestEvent` 钩子、persona 错误文案、并发/流式判定、看门狗、
`_RunnerResultAggregator`、`Metric` 上报……），只是最终拿到的 runner 来自注入方。
`Metric` 上报的名字取自 `str` 字面值，仍是注入方的真实类型名，不会失真。

补丁全部记录原始对象，`terminate()` 会完整还原；重复 `apply()` 是幂等的，并能
回收上一次实例残留的补丁（插件热重载或异常退出的场景）。

---

## 配置

| 配置项 | 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `echo_reply` | 字符串 | `这是 librunner 的 echo 执行器，回复内容可以在插件配置里修改。` | `echo` 执行器回复内容的**默认值**：切换执行方式时它会被写进 AI 页面上的「回复内容」（`agent_runner.config.echo_reply`），之后以那里的值为准；两者都留空时用上面的默认文案 |
| `orphan_runner_policy` | `fallback_local` / `strict` | `fallback_local` | 配置里残留了「本插件注入过、但当前无人注册」的执行器类型时的处理方式。`fallback_local` 回退到内置 Agent 并打印错误日志；`strict` 保持 AstrBot 原生行为（构建 pipeline 时报 `ValueError`） |
| `enforce_prerequisite` | bool | `true` | 前置插件 `astrbot_plugin_libi18nject` 缺失时是否强制处理：开启则尝试自动安装（装不上就停用本插件并把原因显示在插件页的「加载失败插件」）；关闭则只记警告并照常启用（面板文案会有 `[MISSING: ...]`） |
| `prerequisite_repo` | 字符串 | `cxykevin/astrbot_plugin_libi18nject` | 自动安装前置插件时使用的 GitHub `owner/repo`，也可写完整 git 地址（便于内网/镜像） |
| `prerequisite_dir_name` | 字符串 | `astrbot_plugin_libi18nject` | 前置插件在 `data/plugins` 下的目录名（即插件名） |

> 该策略只对「历史上确实由 librunner 注入过」的类型生效（记录在
> `data/plugin_data/astrbot_plugin_librunner/injected_runners.json`），
> 因此手滑写错的 `runner_type` 依然会被 AstrBot 正常拦下。

### 注入方执行器的配置显示在哪里？

两种地方都可以，建议按「谁来改」来分：

1. **执行器自身参数**（服务地址、超时、密钥……）→ 用 `config_default` 声明字段、
   用 `config_metadata` 给文案。librunner 会把它注入成内核同构的
   `ai_group.metadata.<type>_runner` 分组，选到该执行器时面板上就会出现这些
   配置项（直接写进 `agent_runner.config`，内核会以 `provider_config=` 传给
   `runner.reset()`）：

   ```json
   "agent_runner": {
     "runner_type": "myagent",
     "config": {"api_base": "http://127.0.0.1:8080", "timeout": 60}
   }
   ```

   注入方不用自己准备分组文案的 i18n：面板拿到的 metadata 会先经过内核的
   `ConfigMetadataI18n.convert_to_i18n_keys()`，把每个分组/字段的 `description` /
   `hint` / `labels` 替换成 `ai_group.<section>.<field>.description` 形式的 key。
   librunner 会把注入的执行器分组登记给可选的姊妹插件
   [`astrbot_plugin_libi18nject`](#webui-文案是怎么补上的)，由它保证这些分组在
   转换后保持纯文本，注入方无需关心这件事。
2. **插件级设置**（开关、全局策略……）→ 放在注入方插件自己的 `_conf_schema.json`
   里（插件配置页渲染），在 `factory` 或 `reset_kwargs` 里传给 runner，例如
   `echo` 就是「面板配置优先、插件配置兜底」的组合。这部分如果需要多语言，用
   插件官方的 `.astrbot-plugin/i18n/<locale>.json`（`config` 段）即可。

> **「更换执行方式」对话框里每个选项的标题与说明**
> （`aiSettings.runners.<type>.title` / `.description` / `.summary`）来自
> dashboard 的前端静态文案，插件侧无法注入。安装可选的姊妹插件
> `astrbot_plugin_libi18nject` 后，librunner 会把注册时的 `label` / `description`
> 作为这些词条登记过去，由它在响应层补进语言包；没装它时这几处显示
> `[MISSING: features.config.aiSettings.runners.myagent.title]`，只影响观感，
> 功能、下拉框文案与配置项都不受影响。

---

## WebUI 文案是怎么补上的

AstrBot 的 WebUI 里有几处文案没法由插件自己提供，librunner 把它们交给可选的姊妹
插件 **`astrbot_plugin_libi18nject`**：

| 文案 | 来源 | 谁来补 |
| --- | --- | --- |
| 执行器下拉框的选项 | 后端 metadata 的 `labels` | 注入的选项直接用注册时的 `label`，无需词条 |
| 执行器配置分组（标题与字段文案） | 后端 metadata 的 `ai_group.metadata.<type>_runner` | libi18nject 保证这些分组在 `convert_to_i18n_keys()` 后保持纯文本 |
| 「更换执行方式」对话框的标题/说明 | dashboard 前端打包产物 | libi18nject 在响应层把 `features.config.aiSettings.runners.<type>.*` 词条写进语言包 |

librunner 只负责登记内容：`main._refresh_i18n()` 把已注册执行器的 `label` /
`description` 与配置分组交给 libi18nject，并在注册表每次变化时重新登记
（`terminate()` 时注销）。它不修改内核文件，也不碰 `data/dist`。

**没装 libi18nject 时功能完全正常**，只是上面后两处会显示 `[MISSING: ...]` 占位；
装上它并重载 librunner 即可补齐。

---

## 已知限制与风险

- **与内核版本强耦合**：补丁依赖 `AGENT_RUNNER_TYPES`、`CONFIG_METADATA_3`、
  `normalize_agent_runner` 与 `ThirdPartyAgentSubStage.process` 的现有实现。
  升级 AstrBot 后若符号缺失，补丁会**显式报错**并在 `/librunner` 中给出原因，
  不会静默失效。
- **「更换执行方式」对话框的文案**：下拉框的选项文案取自注册时的 `label`，可以
  任意自定义；对话框里每个选项的标题/说明来自 dashboard 的前端静态文案
  （`features.config.aiSettings.runners.<type>.title` 等），只能靠可选的姊妹插件
  `astrbot_plugin_libi18nject` 在响应层给语言包补词条（见
  [WebUI 文案](#webui-文案是怎么补上的)）。未安装它时这几处显示 `[MISSING: ...]`，
  只影响观感，功能、下拉框与配置项都不受影响。
- **前置插件与启动耗时**：`enforce_prerequisite` 默认开启，启动时会先确认前置插件
  `astrbot_plugin_libi18nject` 是否就位；缺失就尝试 `gh repo clone` / `git clone`，
  单条命令最长等 60 秒（`prerequisite.CLONE_TIMEOUT_SEC`），网络不通时会让这个插件
  的加载慢一些。装上后是**就地启用**（不重启 AstrBot 主进程），本插件随即继续启用；
  装不上则停用本插件并把原因报给 WebUI。自动安装每个进程只尝试一次，不会反复克隆。
- **卸载顺序**：卸载 librunner 之前，请先在 WebUI 把执行器切回内置 Agent；否则
  配置里会残留无法识别的类型（AstrBot 会在构建 pipeline 时报 `ValueError`）。
  卸载注入方插件则不受影响：librunner 会按 `orphan_runner_policy` 降级。
- **`config_default` 是白名单**：见 [API 参考](#api-参考)。
- **`RunnerInfo.config_default` 是深拷贝快照**，修改它不会影响注册表。

---

## 故障排查

| 现象 | 排查 |
| --- | --- |
| `/librunner` 显示「内核补丁: 未应用」 | 看「失败原因」：通常是 AstrBot 版本不匹配（符号缺失）；`strict` 之外的错误请附带完整日志反馈 |
| 下拉框里看不到注入的执行器 | 确认注入方插件已加载（`/librunner` 能列出该类型）、AstrBot 版本 >= 4.29.0，并刷新面板页面 |
| 切换执行器后 AI 没有走注入方 runner | 确认 `agent_runner.runner_type` 已保存；启动日志里应有 `librunner: 分发执行器 <type>`（Debug 级别） |
| 日志提示「配置中的执行器 ... 回退到内置 Agent」 | 注入方插件被禁用/卸载了：重新启用它，或在面板里改回内置 Agent |
| 注入方 runner 报错 | 错误详情会出现在聊天回复里（内核的第三方执行器错误包装），同时日志中有 `librunner: ...` 上下文 |
| 选到注入的执行器，AI 页面下方没有配置项 | 该执行器注册时没有声明任何 `config_default` / `config_metadata` 字段，此时不需要展示配置区；补上字段即可，见[注入方执行器的配置显示在哪里](#注入方执行器的配置显示在哪里) |
| AI 页面或「更换执行方式」对话框出现 `[MISSING: ...]` 文案 | 说明当前没有插件在提供这些文案：安装并启用 `astrbot_plugin_libi18nject`，再重载 librunner 让它重新登记即可（只影响观感，功能与配置都正常，见 [WebUI 文案](#webui-文案是怎么补上的)） |
| 选了 `echo` 却没有回复预期文本 | 面板上 `echo` 的「回复内容」优先，其次才是插件配置项 `echo_reply`，都留空时用内置兜底文案；两个来源都是每次请求现取，保存后立即生效 |
| 插件页把 librunner 列为「加载失败」，原因是「前置插件未就绪」 | 前置 `astrbot_plugin_libi18nject` 没能自动装上（网络不通、仓库改名等）：按日志里的错误摘要处理后重载插件；也可以在内网环境把「前置插件来源」换成镜像地址，或关掉「强制前置插件」先用起来（面板文案会显示 `[MISSING: ...]`）。加载失败时插件页的配置面板可能打不开，可直接编辑 `data/config/astrbot_plugin_librunner_config.json` 里的 `prerequisite_repo` / `enforce_prerequisite`，保存后重载插件 |

---

## 开发者自测

本插件靠 monkey patch 接入内核，升级 AstrBot 后请先跑一遍仓库自带的回归自测：

```bash
cd /path/to/AstrBot
uv run python data/plugins/astrbot_plugin_librunner/selftest.py
```

脚本覆盖 143 项检查：runner 类型别名语义、补丁的应用/重复应用/撤销、类型放行与
`config_default` 字段白名单、WebUI 元数据注入（含执行器配置分组的注入/刷新/回收、
「不污染内核自带分组」）、`config_metadata` 校验与类型推断、残留类型降级策略、
执行器 WebUI 文案的构造与向 `astrbot_plugin_libi18nject` 的登记/注销、前置插件的
检测/自动安装（用本地 git 仓库离线演练 gh→git 回退）/就绪判定/装不上时的停止工作
与上报，以及自带 `echo` 执行器的注册/非流式与流式分发/面板配置优先/配置回落/注销，
还有走真实 `ThirdPartyAgentSubStage.process` 的完整分发链路。它会把 `ASTRBOT_ROOT`
指向临时目录，不会读写真实实例的数据；全部通过时退出码为 0。

---

## 参考

- [`PLUGIN_MODIFY.md`](./PLUGIN_MODIFY.md)：本插件改造方案的完整技术分析
  （内核符号核对、时序、坑与风险）。
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
