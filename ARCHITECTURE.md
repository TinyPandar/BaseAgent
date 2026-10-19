# 架构与成果里程碑

内核抽取历史基线：`b536ce2`（2026-10-07），不是当前工作区版本声明。本文件描述当前结构与目标路线；HARNESS_PLAN.md、HARNESS_AUDIT.md 中的分阶段证据保留为历史记录，旧待办不能直接当作当前缺口。

## 当前阶段决策（2026-10-10）

2026-10-19 更新：Project Assistant Phase 1 已交付 RAG 基础设施，Phase 2 已实现应用层 `knowledge_search` 工具包、文件可见范围、引用原文核对、脱敏及离线模型/真实检索回放。代码位于 `applications/project_assistant/`，不改 kernel 或默认 CLI 工具集。现状与边界见 [应用 README](applications/project_assistant/README.md)；下方 2026-10-10 路线及交接中的“RAG 待开发”是历史快照，真实模型质量与 coding 闭环评测仍待实施。

2026-10-19 应用讨论与新会话交接见 [DEVELOPMENT_HANDOFF.md](DEVELOPMENT_HANDOFF.md)：保留已有 coding 应用，候选方向为带 RAG 的个人开发者知识与项目助理；其中建议范围与待定选型不代表已经实现或最终确认。

用户决定将当前同步 harness、能力/配置装配和模型基础设施作为应用开发的阶段基线，提交现有成果。下一阶段优先开发带 RAG 的 Agent 应用与检索/回答评测，面向 AI 应用 / Agent 工程项目展示；具体知识库和应用需求另行确定。本次只保存框架成果与路线，不实现 RAG。

应用通过现有模型协议、ToolPackage、registry 和 middleware 接入；文档解析、切分、索引、检索、重排、引用与业务数据留在 kernel 之外。先实现可测量的应用闭环，再由真实需求推动公共接口改进。

下方通用框架和个人 assistant 路线保留为候选规划，不再构成应用开发的前置清单。常驻 runtime、模型最大在途并发控制、Redis/消息队列、统一容器以及其他执行环境均按实际需求后置；当前令牌桶限制请求速率，不限制在途请求数量。独立 kernel 环境验证与 Anthropic 真实 API 验证仍未补齐，验收范围见 HARNESS_AUDIT.md。

## 架构边界

| 层 | 责任 | 实现入口 |
| --- | --- | --- |
| Kernel | 单任务模型/工具循环、状态、事件、参数验证、取消与 middleware 协议 | `src/baseagent/kernel/` |
| Harness | 会话事务、审批、恢复、预算/用量、子任务、成果验收 | `agent/`、`session/` |
| 能力层 | 可组合工具包、skill 发现/加载与资源访问 | `capabilities/` |
| 执行环境 | 文件、指令、命令、哈希和环境身份 | `backends/`，当前 LocalBackend |
| 宿主 | 模型与能力装配、配置、CLI/UI | `main.py`、`ui/` |

依赖方向：宿主装配能力和 harness；harness 调用 kernel；能力提供 registry 和 middleware。Kernel 不导入能力、持久层、审批或具体执行环境，仅允许标准库和 tools.py 中的 jsonschema。

## 已完成里程碑

1. 同步 harness：会话与工具账本、故障恢复、上下文投影、审批、取消、用量/期限、计划/验证、记忆、顺序嵌套委派、CLI 与可选 UI。证据索引见 HARNESS_AUDIT.md；历史报告不等于当前 checkout 的重新验证。
2. 执行环境接口：WorkspaceBackend 和 LocalBackend；WSL/Docker 尚未实现。本机命令权限不是 OS 沙箱。
3. Kernel Stage 1（b5fa555）：状态和事件契约抽离。
4. Kernel Stage 2（6d121dd）：单任务 kernel 和执行原语抽离。
5. Kernel Stage 3（b536ce2）：同步 run_agent 经 iter_agent 执行；旧状态格式、持久事件及逐次调用统计由 harness 适配。tests/test_kernel_* 包含依赖边界、执行和冻结基线差分检查。

## 能力层交付顺序与验收

- 工具包：coding、memory、shared-memory 独立选择；宿主授权仍决定实际执行权限；默认 CLI 保留原工具集合。库调用能组合自定义工具包，重名拒绝。
- Skill：显式配置目录，发现名称/描述，按需加载 SKILL.md，读取包内资源；不自动运行脚本，不扩大权限。目录内容的摘要纳入 registry 契约，未完成会话恢复检查配置变化。
- 运行入口：CLI 选择工具包和 skill 目录；middleware 将目录信息送给模型，加载结果进入正常工具消息、上下文预算和账本。
- 验证：真实工具 dispatch、脚本模型闭环、权限/路径边界、内容变更恢复拒绝、kernel 边界和全量回归。

本地能力与配置装配已有首轮实现，后续优先级以本文件“目标路线与验收”章节为准。MCP 工具适配、外部 skill 分发/安装、其他执行环境按真实应用需求后置，不作为当前默认主线。

## 能力层首轮成果（2026-10-09）

实现入口为 `capabilities/assembly.py` 和 `capabilities/skills.py`。三个内置包可独立选择，应用可用带版本的 ToolPackage 注册业务 handler。默认 CLI 保持原工具集合。CLI 增加 --tool-package 与 --skills-dir；目录摘要纳入工具契约，skill 列表由 middleware 投影到模型请求，正文和资源由工具按需读取。examples/skills/coding-check 提供可运行的目录格式示例。

首轮选择目录内容快照，保证同一次运行内资源一致，并复用现有恢复漂移检查。实时读取能立即看到文件修改，但会引入正文/资源版本混用，需要额外的运行中漂移契约；因此后置。元数据先采用明确的单行 name/description 格式，避免新增 YAML 解析依赖；完整 YAML、多行元数据和更多 skill 标准字段后续可扩展。脚本通过已授权命令工具执行，skill 加载本身仅提供文本。工具包目前逻辑分组复用原有 handler，后续可把注册实现拆到独立模块，而不改变应用装配接口。

自动验证使用脚本模型，不包含新的付费 provider 请求。tests/test_capabilities.py 检查默认工具兼容、权限、业务扩展、skill 快照/路径/元数据/体积边界、真实 Windows 目录联接拒绝、CLI 装配及持久暂停/恢复。现有 kernel 边界/差分专项 35 项通过；六场景离线评估通过；wheel 内三个能力模块已与源码逐字节比较。完整回归结果见下方。

最终 checkout 全量回归：417 项，178.368 秒，退出码 0。命令：`.venv/Scripts/python.exe -B -m unittest discover -s tests -v`。本机日志 `.baseagent/capability-final-tests.log`，六场景报告 `.baseagent/capability-evaluation.json`，均为忽略产物，不随仓库分发。新增能力专项 7 项全部通过；`git diff --check` 通过。

## Config 驱动装配（2026-10-09）

`config.py` 读取 version 1 严格 JSON，提供 workspace、capabilities、permissions、run、policies 五组宿主默认值。`--config` 选择配置文件，CLI 显式选项覆盖默认值；列表整体替换，配置相对路径基于配置文件目录。配置引用已注册的内置模组名，不执行动态 Python 导入。`--list-tool-packages` 可在无数据库/模型访问时列出可选模组。examples/harness.readonly.json 与 examples/harness.coding.json 是可直接使用的启动配置。

配置不会自动接受恢复漂移，也不会保存 API key。tests/test_config.py 使用真实 harness、脚本模型和 SQLite 验证 config 启动、skill 自动装配、暂停/恢复、模组变化拒绝且 checkpoint 不变；另覆盖 CLI 覆盖权限和列表、路径解析、配置边界及无凭据模组发现。

Config 轮次全量回归 423 项通过，176.812 秒，退出码 0，日志 `.baseagent/config-full-tests.log`；配置专项 6 项通过（含超大时间值拒绝），wheel 构建通过且 config.py/main.py 与源码逐字节一致；格式检查通过。本轮无真实 provider 请求。

## 通用框架目标（2026-10-09 对齐）

BaseAgent 当前仓库同时容纳单任务内核和已有同步 harness；baseHarness 是可复用底座的目标定位，不代表已经拆出独立包或仓库。目标是让个人的新 agent 项目复用稳定内核、模型适配器、工具与配置装配，保持流程白盒、可理解、可修改，减少重复实现。个人 assistant 是第一个应用，第二个实际 agent 的迁移用于验证边界。

科研检索、多个项目的 spec/进度维护、日程记录和提醒是应用需求，不进入 kernel。项目维护的重点是能恢复上下文、明确下一步并完成项目，不能仅用追加日志代替项目看板。

### 分层约束

- **Kernel**：只负责跑一个任务，包括模型协议、工具注册/验证与异常隔离、上下文投影、可序列化执行状态、事件、钩子、协作取消及运行限制。只依赖自身、标准库和 `kernel/tools.py` 中的 jsonschema；不导入 runtime、能力层、具体模型 SDK、SQLite、审批、记忆发布或子任务调度。
- **扩展与装配**：复用现有 `llm/`、`middleware/`、`capabilities/`、`config.py`，承载具体模型、录制/回放、工具包、skill 和配置。优先沿用现有目录/API，不为匹配方案里的示意名称再建一套 `models/` 或 registry。Skill 渐进加载正文与资源；配置引用已注册能力，不执行远程动态代码。热加载仅考虑开发模式。
- **同步 Harness**：现有 `agent/`、`session/` 保留会话事务、审批、账本、预算、恢复、顺序委派和成果验收，作为兼容外壳。已有能力不必因新分层重写。
- **Runtime（规划）**：常驻接收、任务状态库、队列/worker、确定性进度、取消、暂停/恢复、定时提醒。通过外壳或 kernel 公共 API 运行任务，不能要求 kernel 反向导入调度层。持久会话与调度任务分别建模，不能把现有会话列表视为异步 TaskStore。
- **应用（规划）**：research、项目看板、schedule 等能力与业务数据，依赖框架。先用显式命令路由；LLM Router、并行子任务、分布式队列、插件市场按需要引入。

Kernel 的迭代事件表示逻辑包装操作；实际重试调用数看状态计数和持久事件。Runtime 不用事件对数估算费用，不让模型随意生成进度百分比。同步 HTTP/任意 Python handler 的取消仍受协作和传输超时约束，不承诺强杀或撤销副作用。

## 目标路线与验收

下表区分已实现的基础设施与后续工作，当前状态以各行末尾和最新交付记录为准。附件中的固定分支、提交号、410 项测试等是历史执行快照；实施时重新读取 HEAD、worktree、依赖及测试基线，不能照抄为当前前置条件。

| 顺序 | 目标与范围 | 验收门槛与当前状态 |
| --- | --- | --- |
| 1 | 收尾内核独立性与真实入口验证 | AST 边界和冻结基线差分已有历史证据；另需仓库外最小包树、干净 venv 仅安装 jsonschema 及传递依赖，验证导入来源和 sys.modules；真实模型走迭代/同步带工具场景并检查状态 JSON 往返、取消边界和资源清理。尚无本轮隔离/真实验证证据，不能宣称已完成；合并/tag 另按明确授权执行。 |
| 2 | 模型调用录制/离线回放 | 新增 kernel 外 testing 支持，在模型协议层记录版本化 JSONL；默认 replay，请求不一致、超出调用数、未消费记录、损坏文件和未知版本明确失败；规范化请求保留语义、只移除明确的非语义字段，以脱敏后哈希严格匹配；响应和异常可重放，状态和事件（忽略时间戳）一致。库级录制/回放已实现；三个真实 DeepSeek 合成录像已录入并通过脱敏扫描与离线回放。 |
| 3 | 模型适配器与标准错误/重试/限流 | 扩展现有 OpenAI 兼容适配器以覆盖 DeepSeek，增加 Anthropic；统一消息/工具/结果/usage 与错误分类，复用已有 SDK 和重试语义，补可取消退避、Retry-After、线程安全限流及共享离线契约测试。库级 OpenAI 兼容/Anthropic、标准错误、重试/Retry-After/限流已实现；CLI/config 选择不在本轮范围。 |
| 4 | 打包与新项目起手式 | 已有 pyproject、wheel 和 README 最小 kernel 示例；补明确公共 API、工具/配置/测试模板，并让第二个实际 agent 迁移验证复用。包级独立性未验证前不宣称整个发行包只有 jsonschema 依赖。 |
| 5 | 统一能力装配 API | 现有工具包/skill 与严格 JSON CLI 配置是基础；核对库级 build_agent(config) 是否需要补齐，明确配置校验、重名拒绝、权限和恢复漂移契约。当前不等同于已实现统一库级入口；无实际需求不新增 YAML 依赖。 |
| 6 | 最薄常驻 runtime | 单机 SQLite TaskStore + 状态机 + 单 worker，先支持 submit/list/status/cancel；阻塞执行隔离出事件循环，等待输入释放 worker，重启将运行任务标 interrupted，未知副作用先核验。验收长任务执行期间入口仍响应、状态持久化、取消与恢复不重复副作用。当前未实现；多 worker/路由/子任务树后置。 |

录制/回放要求：可提交录像仅来自写死的合成场景（直接回答、一次工具调用、工具错误后纠正）；不从真实个人会话录制。请求、响应、异常和 diff 均需脱敏，覆盖敏感环境变量值、密钥/Bearer 模式、主目录路径、邮箱及自定义规则；记录脱敏次数。私有录像另置忽略目录。无密钥时交付录制脚本并标明真实录像待补，禁止把假模型录像当成真实模型证据。离线回归不得隐式发网络请求或付费录制。

适配器要求：认证/请求/上下文超长错误不重试；限流、暂时性网络/服务错误、超时才按策略重试。迭代事件仍按逻辑操作、计数和持久记录按实际尝试。取消等待需可中断，repr/日志/异常不得泄漏凭据，未知用量沿用现有核验契约。仅在确有需要时给 kernel 增加最小模型错误或响应序列化契约，保持依赖白名单。

### 个人 assistant 的应用范围

- 科研：作为耗时 agent 任务后台执行，保存产物与可追踪进度。
- 多项目看板：每项目一个目录，`SPEC.md` 保存一句话目标、初始需求、完成定义和不做范围，`PROGRESS.md` 追加进展；SQLite 保存元数据/索引。状态为 idea/active/paused/done/abandoned，保存一条可执行下一步、仓库路径、最近活动时间，放弃可记录原因。支持恢复简报、停滞提醒和每周回顾；active 数量上限可配置，不在框架里硬编码为 2 或 3。
- 日程：结构化记录到期时间和提醒，由 runtime 定时器处理；普通记录/查询不必调用 LLM，也不必伪装成 agent 任务。

项目状态与执行任务状态是不同契约。Commit 时间仅作为活动信号之一，不能证明没有未提交进展。LLM 用于 spec 整理、恢复简报和回顾总结，业务存取和到期判断保持确定性。CLI 优先，本地网页和其他消息入口后置。

### 后续开发约定

先读相关源码，最小改动，每个阶段可独立运行并给验证证据；现有同步 API 保持兼容。新增第三方依赖或重大边界变更先说明并确认；不为变绿删减既有测试断言，测试时序修复应保留原语义并提供原因。已授权范围内连续执行，普通阶段不再默认“先计划等确认”。遇到真实阻断报告原因，不伪造通过，也不将后续功能顺带实现。

全量测试使用 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`，测试数量以当前发现为准。记录当前版本、命令、结果及离线/真实验证区别；历史全绿不替代当前复验。密钥不得进入文档、日志、录像或提交。交付说明改动、验证、未完成项和已知限制。

2026-10-18 用户持续授权：以后每次完成开发及相应验证后，默认提交本次任务成果并推送到 GitHub 当前分支的既有远端，无需逐次确认；用户当次另有指示时优先遵守。提交前检查改动范围与敏感信息，不夹带无关改动；推送后核对远端状态，失败时明确报告。合并、打 tag 和强制推送仍需另行授权；不从附件示例推导额外授权。

## 模型基础设施交付（2026-10-10）

新增库级 `OpenAICompatModel`、`AnthropicModel`、`CassetteModel`、`Redactor`、`RateLimiter` 与 `RateLimitMiddleware`；扩展既有 `RetryMiddleware` 和 `ModelResponse`。CLI/config 与之前未提交的能力层保持原状，不新增依赖、runtime、提交或推送。

Kernel 增量仅为标准库模型错误类型/导出，以及 ModelRequest/ToolCallRequest 上默认 None 的剩余期限回调。同步外壳传入实际作用域期限；等待到期直接中断，由同步外壳核验作用域，保持 DEADLINE_EXCEEDED、调用账本和祖先预算语义。新版适配器输出字典响应；旧 Model 构造与 SDK 消息返回保持兼容。成功响应在取消到达后仍先记用量，后续执行停止。

离线验收入口：tests/test_cassette.py、tests/test_model_adapters.py、tests/test_model_middleware.py、tests/test_real_cassettes.py；真实录制入口：scripts/record_cassettes.py（固定合成任务，最多 9 次实际请求）。基线 423 项通过；最终全量结果记录在 HARNESS_AUDIT.md。初次实现时缺少凭据，真实回放测试曾跳过；随后用户提供凭据文件，已用 6 次真实 DeepSeek 请求补齐三个合成录像，均通过扫描与断网回放。凭据仅加载到子进程，未复制到仓库。

录入前全量验收：462 项，183.621 秒，459 项通过、3 项跳过。真实录像录入后复验：462 项全部通过、无跳过，208.807 秒，退出码 0。离线 wheel 构建成功，81 个 Python 文件逐字节核对和 wheel 公共 API 导入通过；详见 HARNESS_AUDIT.md 最新验收章节。

## 指标与性能测量（2026-10-10）

指标定义、硬护栏、初次离线耗时基线及复测方法见 [PERFORMANCE.md](PERFORMANCE.md)，复测入口为 scripts/benchmark_harness.py。本机六组合各 100 个计时样本全部满足断言，未调用 provider；持久路径的耗时差异仍需逐阶段剖析。微基准不等于真实任务质量、生产 SLO 或常驻吞吐。本轮只新增设计/基准脚本和文档，未改变执行内核。
