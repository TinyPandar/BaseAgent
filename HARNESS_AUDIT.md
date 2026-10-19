# Harness completion audit

> 本文记录各轮验收证据；当前架构和里程碑见 [ARCHITECTURE.md](ARCHITECTURE.md)。历史测试和忽略目录下的报告不代表当前 checkout 已重新验证。

## Project Assistant Phase 2（2026-10-19）

本轮从 `ca9f431` 的干净工作区接续应用开发，范围为 `knowledge_search` 工具包、来源可见性、结果脱敏及录制/回放。应用实现位于 `applications/project_assistant/access.py`、`tools.py`，复用现有 `ToolPackage`、registry、run_agent、工具策略和配置漂移检查；没有修改 kernel 或增加第三方依赖。

- 开发前全量基线：477 项通过，218.732 秒，退出码 0，日志 `.baseagent/phase2-baseline-tests.log`。
- 最终全量回归：`.venv/Scripts/python.exe -B -m unittest discover -s tests -v`，497 项全部通过，无跳过，235.373 秒，退出码 0，日志 `.baseagent/phase2-full-tests.log`。所有本轮 Python 实现修改均先于此运行；随后仅补充文档。`git diff --cached --check` 通过；13 个待提交文件未检出敏感环境变量原值，索引、数据库和日志保留在忽略目录内。
- RAG 专项：`.venv/Scripts/python.exe -B -m unittest discover -s tests -p "test_rag*.py" -v`，35 项通过，3.592 秒，退出码 0，日志 `.baseagent/phase2-rag-tests.log`。其中新增 20 项覆盖宿主文件 ACL、backend 保护路径、路径穿越/Windows junction 拒绝、过期/伪造引用过滤、脱敏后才持久化/录制、结果限额、策略拒绝/审批/取消、恢复配置漂移及查询 CLI。
- `.venv/Scripts/python.exe -B scripts/demo_knowledge_search.py`：7 项检查全部通过。临时合成语料、Mock embedding、真实 Chroma 查询、确定性模型响应；record 后断网 replay，检索在 replay 中仍实际执行，状态一致且写权限保持关闭。脚本生成的临时假模型录像不会作为真实 provider 录像入库。
- 仓库语料 CLI smoke：`build_knowledge_base.py --mock-embedding --output-dir .baseagent/phase2-smoke-index --metadata-file .baseagent/phase2-smoke-corpus.json` 成功入库 147 个文件、1916 个片段，版本 `ab74db3b4a8de83c`。随后 `query_knowledge_base.py "kernel architecture" --mock-embedding --index-dir .baseagent/phase2-smoke-index --top-k 3` 成功返回 3 条带有效行号的证据。日志为 `.baseagent/phase2-build-smoke.log`、`.baseagent/phase2-query-smoke.log`；这些是入库/接口证据，Mock embedding 结果不代表语义检索质量。语料统计仅对应当时的工作区快照。

边界：knowledge ACL 与 coding ToolPolicy 分别由宿主配置；固定包背后的索引不应静默变更，重新入库需以新语料版本重新装配。默认排除私有路径且出口尽力脱敏，不保证识别所有未知凭据格式，也不自动清理索引中的历史原文。工具只核对被检索片段及当前引用位置，不保证整个索引快照始终最新。底层 LocalBackend 的访问竞态边界仍适用，不是 OS 沙箱。引用/参考标记不能保证模型回答正确或免疫提示词注入。本轮没有付费 API 调用；真实回答质量、检索质量评测和受控 coding 闭环留待后续。

> 2026-10-09：后续范围已对齐可复用框架路线，见 ARCHITECTURE.md 的“目标路线与验收”。当时的完成声明仅覆盖同步 harness 约定范围；2026-10-09 当轮只改文档、未重跑代码验收。模型基础设施的后续实现与复验见下面 2026-10-10 记录，常驻 runtime 和个人 assistant 仍未实现。

## Harness 阶段基线提交验收（2026-10-10）

用户授权提交现有成果，下一阶段转向带 RAG 的 Agent 应用。此次保存同步 harness 上的能力/skill 装配、JSON 配置、模型适配器、统一模型错误、重试与共享速率限流、严格录制/回放、三个真实合成录像、回放演示及性能测量脚本。未实现新的 RAG、runtime、并发准入或 Redis/队列；后续框架工作由应用需求驱动。ARCHITECTURE.md、HARNESS_PLAN.md 与 README 已同步这一阶段选择。

- 当前源码全量离线回归：`.venv/Scripts/python.exe -B -m unittest discover -s tests -q`，462 项全部通过，无跳过，196.201 秒，退出码 0；日志 `.baseagent/harness-checkpoint-tests.log`。
- `scripts/demo_replay.py` 通过：禁止 socket 连接，读取两个保存的模型响应，实际执行一次本地加法工具；变更请求后明确拒绝回放。本轮未发起付费模型请求，真实录像来源仍为下方已有录制记录。
- `scripts/benchmark_harness.py --samples 5 --warmups 1 --output .baseagent/checkpoint-performance-smoke.json` 通过：kernel、内存 harness、SQLite harness 的直接回答/单工具六组场景各 5 个样本全部成功。本轮仅验证脚本可用，不把小样本结果视为生产性能指标。
- `uv build --wheel --offline --out-dir .baseagent/checkpoint-dist` 成功；wheel 中 81 个 Python 源文件与工作区逐字节一致，README 元数据一致，隔离解释器从 wheel 实际导入能力、配置、模型、中间件与回放公共 API 通过。
- 待提交文件扫描未发现用户所提供凭据中的敏感值；三个合成 JSONL 的脱敏扫描通过。凭据、私有录像、数据库、测试日志和构建产物均保留在忽略范围内。`git diff --cached --check` 通过。

本次为本地阶段基线，不发布包、不推送、不打 tag。Anthropic 真实 API、kernel 仅依赖 jsonschema 的独立运行环境验证和生产并发/负载验证仍未完成；这些边界不阻断用户选择的本地应用开发阶段。

## 真实合成录像补证（2026-10-10）

用户提供凭据文件后，仅将 DeepSeek 配置加载到本次子进程环境，未复制 .env 到仓库、未输出密钥。scripts/record_cassettes.py 使用固定合成提示词完成 6 次真实模型请求（上限 9，无自动重试），三个场景通过全部断言/脱敏扫描/离线回放后才发布：

| 录像 | 实际模型调用 | 工具调用 | 直接验证 |
| --- | --- | --- | --- |
| tests/cassettes/deepseek_direct.jsonl | 1 | 0 | 固定直接回答 |
| tests/cassettes/deepseek_tool.jsonl | 2 | 1 | 一次加法工具后回答 42 |
| tests/cassettes/deepseek_correction.jsonl | 3 | 2 | 先除零失败，读取错误后下一次模型调用纠正为除以 2，再回答 42 |

三场景报告未知用量均为 0；使用附件凭据值的隐私扫描通过，socket 连接被禁止时三个场景仍能完整回放。文件 hash、调用/用量及扫描/回放结果记录于忽略产物 `.baseagent/real-cassette-evidence.json`，不含凭据。

录入后的全量回归：462 项全部通过、无跳过，208.807 秒，退出码 0。测试进程同样仅从附件加载配置供隐私扫描使用；单元/本地 HTTP 测试和真实录像回放均按已有离线测试入口执行。日志 `.baseagent/model-real-final-tests.log`。本轮未修改 Python 源码；新增三个真实录像并更新状态文档。离线 wheel 构建、81 个 Python 文件逐字节对比和当前 README 元数据核对通过，`git diff --check` 通过。

DeepSeek 真实录像验收已完成；Anthropic 仍只有本地 HTTP 契约验证，kernel 的单独隔离环境/独立入口验证仍属于后续里程碑。本轮无提交、合并或推送。

## 模型基础设施验收（2026-10-10，真实录像录入前）

当前新增范围：库级严格录制/回放、OpenAI 兼容与 Anthropic 适配器、模型错误分类、Retry-After/可选抖动重试和共享令牌桶限流。复用既有同步/预算/工具安全重试机制；CLI/config 与既有未提交能力层未改动，无新增依赖、提交、合并、tag 或推送。

| 契约 | 实现与直接证据 |
| --- | --- |
| 请求严格匹配、离线回放、用量/状态/事件一致 | testing/cassette.py；test_cassette.py 的 kernel 四场景、同步重试、同步/迭代共用请求与预留/估算测试；请求 hash 保留消息、工具 schema、生成参数和工具 ID，未知异常通过固定白名单恢复 |
| JSONL 完整性及发布 | 缺文件/错误版本/重复字段/无效 usage/hash 拒绝；调用过多/不足明确失败；大小限额、无覆盖原子发布、失败关闭、真实并发使用拒绝；test_cassette.py |
| 隐私与凭据 | 请求/响应/异常脱敏、私有目录忽略、短 secret/自定义规则测试；adapter repr/日志/traceback 不含假凭据；含换行的错误凭据预先拒绝；test_cassette.py、test_model_adapters.py、test_real_cassettes.py 的扫描 |
| 两厂商模型契约 | llm/adapters.py、llm/model.py、llm/response.py；共享本地 HTTP 测试覆盖文本、工具、usage 缺失、429/500/529/504/401/上下文错误/坏 JSON/超时；Anthropic 多工具结果分组和缓存输入计数；无 SDK 隐式重试 |
| 重试/限流/期限/实际账本 | middleware/retry.py、ratelimit.py、waiting.py；假时钟验证 Retry-After、抖动、取消、期限；共享并发限流；真实 SQLite 逐次尝试事件；限额不绕过；直接使用中间件到期也不调用 handler；test_model_middleware.py |
| 取消后响应的用量 | 两适配器收到成功响应后由 harness 先记 usage，再停止后续执行；test_model_adapters.py 验证 cancelled、总用量 6、未知调用 0 |
| 固定合成录像脚本 | scripts/record_cassettes.py：最多 9 次请求，三个场景全部断言/脱敏扫描/回放通过才发布，不覆盖；缺密钥、网络失败、录制失败均不发布；脚本端到端假模型只写临时目录，不冒充真实 fixtures |

前置基线：423 项通过，197.598 秒。最终 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`：462 项，183.621 秒，退出码 0，459 项通过、3 项跳过。跳过项仅为缺少真实 DeepSeek 录像的 direct/tool/correction；不存在隐式模型请求。日志 `.baseagent/model-final-tests.log` 为忽略产物。新增 39 项（含 3 项真实录像待补），原有回归未删减。

`uv build --wheel --offline` 成功；wheel 的全部 81 个 harness Python 文件与当前源码逐字节相同，从该 wheel 成功导入新增模型、回放、脱敏、重试及限流 API。`git diff --check` 通过。Kernel AST 边界/冻结基线差分包含在最终全量中；这些证据不替代未来仅 jsonschema 环境下的真实独立性验证。

录入前的真实验证限制：当时缺少 DeepSeek 配置，脚本报告待补、三个录像测试跳过。这一 DeepSeek 缺口已由上方真实录入记录补齐；下述记录保留为初次实现的历史证据。Anthropic 本轮仅经过本地 HTTP 契约验证，无真实 API 冒烟。同步 I/O 仍受协作和传输超时约束；脱敏后的匹配不能区分同一占位符代表的不同私密值。完整用法和边界见 README.md。

状态：2026-10-05 用户明确接受最后一次请求可能超预算，新增分词器估算预算模式。严格输入上界计数器不再是用户当前要求的阻断条件；原严格模式作为可选能力保留。其余 Windows 机械能力按下文逐项审查，新增模式最终全量门槛通过，当前约定范围已完成，适用边界见下文。范围来自 `HARNESS_PLAN.md` 的能力表、`SUBTASK_DESIGN.md` 的调用/状态/恢复契约和十项验收场景，以及 README 的实际入口。以下索引明确实现、直接断言与适用边界；测试数量不是完成证明。

此前门槛（不覆盖新增估算模式）：最终源码 347 项全量测试通过（185.315 秒，退出码 0），命令 `.venv/Scripts/python.exe -m unittest discover -s tests -q`。wheel 中全部 harness Python 文件与当前 checkout 字节一致；wheel CLI 六场景共 35 项检查、构建产物专项 6 项检查和当前真实预授权委派 13 项检查均通过。源码摘要 `a0c17577a285948e50d9eb9f04a2c85f9f293f0eb29db70a301597b4f6bb450f`，报告路径及执行边界见下方。

## 整体能力与文件对应

| 计划要求 | 实现位置 | 验证入口 | 审查状态 |
|---|---|---|---|
| 模型/工具循环、LangChain 风格链式中间件 | `agent/agent.py`、`middleware/middleware.py` | `tests/test_harness.py`、`tests/test_execution.py` | 已核对注册顺序/逆序回返、模型与工具短路/改写、实际重试计数、工具预算和同轮重复 ID 在执行前拒绝；当前全量 gate 通过 |
| 工具 schema、错误、限额、超时与进程清理 | `tools/registry.py`、`tools/result.py`、`tools/process.py`、`tools/_windows_job.py` | `tests/test_execution.py`、`tests/test_command_deadline.py`、`tests/test_cancellation.py` | 已核对执行前 schema/有限 JSON 检查、结果契约/字节限额、双流持续排空、真实超时/取消/正常退出后代清理和剩余期限；本轮补直接库入口校验及深层结果错误归一化，见下方边界 |
| 多轮持久化、根锁、原子工具账本、保守恢复 | `session/store.py`、`session/tasks.py` | `tests/test_sessions.py`、`tests/test_tasks.py`、迁移专项 | 已核对多轮计数/缓存、同会话锁排他及硬退出释放、pending/running/completed 条件转换、结果/历史/事件事务回滚、未决副作用阻止自动重跑和新轮次；旧版本迁移断言见下方 |
| 模型输入裁剪、完整历史、摘要与私有记忆 | `agent/context.py`、`tools/memory.py` | `tests/test_context.py`、`tests/test_memory.py` | 已核对成组工具配对、UTF-8 实际输入预算及中间件后重新检查、原始历史不改、摘要前缀来源摘要匹配/失效回退、私有笔记 CAS/隔离/原文分页；摘要语义正确性仍由使用方核查 |
| 显式同工作区共享参考记忆 | `session/knowledge.py`、`tools/shared_memory.py` | `tests/test_shared_memory.py`、`tests/test_node_memory.py` | 已核对独立发布能力、工作区命名空间、并发 CAS/撤回墓碑、来源笔记和消息变化/删除提示、当前节点写权限与历史节点只读来源、事务回滚及导出/备份；参考文本不升级为指令或验收证据 |
| 恢复配置/已观察工作区漂移 | `session/compatibility.py`、`middleware/repository.py` | `tests/test_context.py`、`tests/test_coding.py` | 已核对工具/运行配置/旧契约拒绝在副作用前发生；模型返回期间指令变化保持账本 pending；接受工作区变化不放宽旧编辑哈希；库调用方需配置中间件与实现版本 |
| 预算、使用量、重试、取消、请求预留 | `agent/scope.py`、`middleware/retry.py`、`llm/reservation.py`、`llm/bounds.py` | `tests/test_scope.py`、`tests/test_retry.py`、`tests/test_provider_bounds.py`、`tests/test_task_cancellation.py` | 已核对实际派发前祖先/本地额度检查、直接/子树计费、未知量阻止再调用、真实期限/取消命令、安全重试与重定向、预留原子保存/退款/超上界暂停；新增估算模式及实际结算/超额停止专项通过；严格计数器缺少服务方保证，作为可选模式边界保留 |
| 有序事件、脱敏、追踪与保留策略 | `agent/events.py`、`session/eventlog.py` | `tests/test_usage_events.py`、`tests/test_eventlog.py` | 已核对提交顺序/事务、分页及重连、跨会话序号空隙、裁剪标记、预览摘要复查/根锁/回滚、真实 CLI 实时增量和导出/备份一致；元数据无 prompt/参数/结果正文，非逐 token 文本流 |
| 运维：检查/导出/备份/迁移/删除/清理 | `session/maintenance.py`、`main.py` | `tests/test_maintenance.py`、`tests/test_tasks.py` | 已核对快照并发一致、在线备份完整性/重开、失败不发布/不覆盖、保护存储路径、锁与未决节点保护、删除回滚及年龄复查/分页；迁移证据见下方 |
| coding：指令、原子局部编辑、计划、实际验证、完成门槛 | `tools/instructions.py`、`tools/editing.py`、`tools/workflow.py`、`agent/completion.py` | `tests/test_coding.py`、`tests/test_completion.py`、离线 `coding`/`delegation` 场景 | 已核对范围指令、精确字节保留与失败全不应用、哈希/合作锁/新建不覆盖、计划 CAS、真实命令及过期证据、当前轮次最新成功命令与交付哈希；用例与实际六场景产物见下方 |
| 参数权限与审批、不升级能力 | `tools/policy.py`、`session/task_recovery.py` | `tests/test_policy.py`、`tests/test_scoped_policy.py`、`tests/test_task_recovery.py` | 已核对参数规则首匹配/组合条件/JSON 类型/精确 argv、目录链接拒绝、实际终端派发重新检查、精确请求/策略/轮次审批绑定、拒绝不执行及许可不扩能力；根和节点跨进程审批证据保留 |
| 顺序组合、嵌套、节点恢复和内存模式 | `agent/subtasks.py`、`session/memory.py`、`session/task_cancellation.py` | `tests/test_subtasks.py`、`tests/test_memory_store.py`、`tests/test_task_cancellation.py` | 已逐项核对十项契约：稳定节点/同名隔离、创建前深度数量限制、根锁/原子祖先账本、严格权限交集、原始期限/取消、暂停/精确恢复/单次缓存交付、未知副作用人工核验、整树运维及隐式/显式内存模式；证据和边界见下表 |
| 质量门槛、CLI、离线与真实验证 | `evaluation.py`、`main.py`、`examples/smoke_delegation.py`、`pyproject.toml` | `tests/test_evaluation.py`、六场景 CLI、真实 smoke 报告、wheel 构建/导入验证 | 已核对评估命名断言/失败不能冒充通过/原子报告、CLI 不依赖 provider 的管理入口、wheel 实际导入后的 CLI 和六场景、当前源码真实预授权委派；最终 347 项全量终端退出码 0 |

## 子任务十项验收的直接证据索引

以下条目指向实际断言，不以名字或绿灯数量替代其覆盖范围；本轮新增项的最终全量门槛记录到计划文件后才构成本轮证据。

| 编号 | 当前直接证据 | 边界/待查内容 |
|---|---|---|
| 1：实际读文件、统计与一次交付 | `evaluation._delegation` 的真实读取、直接/子树计费与配对检查；真实 `.baseagent/delegation-smoke-de4d570b2236462aa62556a77f6fb314/report.json` | 真实报告对应生成时的源码；不是任意模型决策质量证明 |
| 2：祖先/本地额度及深度/数量 | `test_scope.py` 的实际准入；`test_tasks.py::test_depth_and_quantity_limits_prevent_node_creation` | 已核对 `ExecutionScope` 终端派发前准入及 `tasks.create` 的 INSERT 前深度/数量检查；限制委派工具次数另在工具准入检查 |
| 3：预留/退款/未知量、请求中硬退出 | `test_subtasks.py::test_hard_exit_during_child_model_retains_ancestor_quote_and_unknown_usage`；`test_task_recovery.py`；真实预授权 smoke 的整树报价与退款 | 脚本模型硬退出为控制实验；不能推断真实失败请求的账单用量 |
| 4：取消真实子命令、原始期限 | `test_task_cancellation.py` 的根和节点 CLI 取消真实委派命令；`test_scope.py::test_original_root_deadline_limits_actual_child_command` | 同步 HTTP 仍依赖超时/协作检查，非任意 Python 强杀 |
| 5：子副作用硬退出和缓存交付 | `test_subtasks.py::test_hard_exit_after_delegated_child_effect_requires_reconcile_without_replay`；`test_hard_exit_after_completed_child_recovers_cached_delivery` | 外部副作用不加入 SQLite 事务；结果未知时必须核验 |
| 6：跨进程审批、精确请求、不提前执行 | `test_subtasks.py::test_actual_delegation_node_approval_and_resume_across_processes`；`test_task_recovery.py` 的节点/策略/参数绑定 | 审批进程仅记录决定，恢复执行必须另行触发 |
| 7：契约漂移与能力交集 | `test_subtasks.py` 的配置拒绝、父 deny、工具集合不能扩张；`test_node_memory.py` 的共享发布能力 | 不构成操作系统沙箱 |
| 8：同名/重复 call ID 隔离与循环 | `test_same_named_nested_tasks_with_different_prompts_are_isolated`；相同名称+相同内容循环拒绝；嵌套不重复计费 | 内容持续变化由深度/数量限制兜底，未声称语义循环识别 |
| 9：树运维、迁移、失败事务 | `test_tasks.py` 的迁移、导出/备份/删除、事件失败回滚；`test_task_cancellation.py` 的取消请求导出/备份及事件回滚 | 已核对导出 SQL 不限制当前根轮次，包含历史节点/账本/预算；备份整库、删除级联、未决节点阻止清理；旧 schema 迁移证据见整体索引 |
| 10：无 provider 管理、组合评估、真实委派 | 节点管理 CLI 专项；六场景离线评估；真实普通与预授权 smoke 报告 | 当前离线报告/全量 gate 已更新；真实报告保持其生成时源码边界 |

### 当前源码的构建与真实执行证据

- harness Python 源码摘要：`a0c17577a285948e50d9eb9f04a2c85f9f293f0eb29db70a301597b4f6bb450f`；构建后逐个比较 wheel 中全部 harness `.py` 与当前 checkout 原始字节一致。README/计划/测试不属于该运行代码摘要范围。
- `uv build --wheel --out-dir .baseagent/package-audit-50431a382992470eaac80007a4d22696` 退出码 0，wheel SHA-256 为 `77639c70fea69c19cdb98fe829bf8f6334b6eae5e0eccecd4079ddffeaed8013`。包实际解压并在 `python -I` 子进程优先导入，断言 import 来源在解压路径内，然后执行 `baseagent.main()` 的 help 和六场景评估，退出码均 0；报告 `.baseagent/package-audit-50431a382992470eaac80007a4d22696/report.json` 的 6 项检查全部通过。测试复用本地现有依赖，未证明所有第三方未来版本兼容；产物未发布到外部仓库。
- wheel CLI 六场景 35 项检查全部通过，报告 `.baseagent/package-audit-50431a382992470eaac80007a4d22696/wheel-evaluation.json` 的源码摘要与上项相同；没有创建测试 cwd 的默认用户数据库。
- `.venv/Scripts/python.exe examples/smoke_delegation.py --preauthorize` 退出码 0，报告 `.baseagent/delegation-smoke-72c7bec360ed4eff85ef26e66e17fd43/report.json` 的 13 项检查全部通过。实际读文件和 marker/字节一致，4 次模型、2 次工具调用；根直接 6007 + 子直接 1129 = 7136 报告 tokens，未知量 0。独立重开数据库核对 4 条 model_started 均有 1048576 预留、根/子预留归零、根委派 attempts 为 1；缓存 CLI 不允许创建适配器/网络仍通过。真实请求使用显式容量上界 profile；没有补齐紧致输入计数器。

## 整体审查的直接证据与边界

- 工具执行：`test_bad_arguments_cannot_reach_handler`、`test_direct_library_nonfinite_or_non_json_arguments_never_execute` 检查副作用列表为空；`test_invalid_runtime_limits_refuse_before_process_or_job_creation` 检查未创建 Popen/Job；`test_excessively_nested_results_return_error_without_crashing_run` 检查结构化失败。双流各写入 500 KB 后只保留各 1024 bytes；超时和正常结束后实际等待并检查后代未写入标记。`test_actual_command_deadline_result_is_durable_and_not_replayed` 核对真实超时、账本结果及恢复调用次数。当前实际环境为 Windows；POSIX 分支经源码核对，未在本次环境运行。Job 在启动后关联，不能保证恶意进程绝无逃逸窗口；任意 Python handler 的强制抢占不在这个实现中。
- 会话恢复：`test_result_and_transcript_commit_roll_back_together` 与 `test_event_and_tool_completion_rollback_together` 注入持久化失败并读取数据库确认回滚；`test_hard_process_exit_preserves_running_ledger_and_releases_lock` 在独立进程写文件后 `os._exit(23)`，恢复时保持未知账本且不再调用工具。`test_resume_pending_batch_does_not_repeat_completed_tool` 检查每项工具只执行一次；同一会话排他锁、不同会话并行锁和新轮次计数保留有直接断言。SQLite 使用 WAL、FULL synchronous 和外键；外部副作用仍无法加入数据库事务。
- 迁移：`test_usage_events.py` 的 v1、`test_cancellation.py` 的 v2、`test_eventlog.py` 的 v3、`test_shared_memory.py` 的 v4、`test_tasks.py` 的 v5 专项通过构造旧版本缺少的新表，重新打开当前 store 并比较已有状态、账本或事件；未来版本 99 被拒绝。它们证明本仓库旧 schema 的迁移契约，不能证明任意损坏或外部改写数据库可修复。
- 上下文/记忆：`test_active_turn_keeps_prompt_and_latest_complete_batch`、`test_orphan_duplicate_or_unfinished_tool_replies_rejected` 核对配对；中文和转义内容按实际 UTF-8 大小检查。`test_terminal_projection_enforces_limit_after_middleware` 证明中间件扩张不能绕过预算；`test_model_mutation_does_not_change_durable_transcript` 证明输入副本隔离。摘要来源改变则回退原始历史，摘要仍受输入预算，原文分页拼接还原完整消息；笔记文本不进入元数据事件。来源摘要仅绑定原文，不证明生成摘要忠实。
- 配置/工作区：`test_tool_drift_refused_before_pending_side_effect_and_state_unchanged` 检查执行次数为零且状态不变；运行配置仅保存摘要，旧无契约会话需显式接受。`test_guidance_changed_during_model_request_blocks_before_tool_starts` 检查工具计数为零且账本保持 pending；`test_workspace_acceptance_cannot_bypass_stale_pending_write_hash` 检查接受漂移后旧写入仍冲突。仅检查已观察文件，不能推断所有未读取文件无变化；库调用方应为实现变化提供 `runtime_config` 并安装 `RepositoryMiddleware`。
- 事件：`test_events_are_ordered_paged_and_exclude_sensitive_bodies` 使用独特 prompt/参数/结果/回答标记，断言均未出现在事件中。`test_cli_follow_and_prune_without_model` 在 CLI 首次输出后提交新事件，检查无遗漏/重复且最新提交可见；`test_prune_transaction_rolls_back_on_metadata_failure` 注入裁剪元数据错误后确认事件未删除。preview digest 变化拒绝应用、最低保留数和 history_lost 标记均有断言。固定元数据的脱敏范围不包括会话原始历史；可信扩展直接调用 `event` 时仍需遵守无正文约束。
- 运维：`test_export_is_one_snapshot_even_if_writer_commits_between_queries` 与 `test_inspection_state_and_ledger_share_one_snapshot` 在读取中提交新状态，断言档案/检查仍属同一旧快照。实际在线备份可重新打开且缓存恢复不执行模型；目标竞态/已有目标/保护路径与备份超时均不发布残缺产物。删除事件失败时会话及子树回滚，清理重新核对年龄和未决节点并跳过持锁会话。原子发布依赖同目录 hard link；JSON 是归档格式，恢复使用 SQLite 备份。
- coding：`test_multiple_edits_are_atomic_and_preserve_other_bytes`、`test_failed_later_edit_or_ambiguous_anchor_applies_nothing` 使用真实文件字节比较；`test_new_file_creation_cannot_replace_racing_creator` 检查新建不覆盖竞争文件。合作锁和暂存期间外部写入被拒绝；非合作写入仍有检查至替换的竞态窗口，不属于操作系统隔离。`test_wrapper_fake_success_is_not_actual_verification`、旧轮次/错误命令/漏跟踪路径/最新失败/最终响应期间文件变化专项均拒绝完成。当前 CLI `coding` 与 `delegation` 场景比较真实文件、运行真实验证命令、绑定交付哈希；模型和用量为脚本模拟。验收覆盖调用方配置的命令及依赖路径，不自动证明任意任务正确。
- 共享记忆：两线程同时发布同 key/revision 仅一项成功；撤回保留新版本墓碑，旧 revision 无法复活文本。来源笔记改写/删除/消息改写/会话删除各有不同状态断言。节点来源精确绑定 task_id 与 node_turn_id，根新轮次后仍可读取历史来源，删除节点后不回退同名根笔记；内存模式不能共享发布。独立发布能力与父策略交集在真实委派工具路径检查。
- 权限/预算：`test_actual_redirect_cannot_escape_argument_scope`、`test_redirected_arguments_do_not_inherit_approval`、`test_redirected_tool_is_rechecked` 证明改写后的终端调用再次检查。真实 Windows 目录链接及额外命令参数被拒绝；审批仅针对精确字符串摘要、策略版本与轮次。`test_root_and_local_call_limits_refuse_before_dispatch`、`test_root_tool_limit_blocks_child_effect_with_pending_record`、`test_inner_redirect_to_unsafe_write_cannot_be_retried` 检查实际调用/副作用次数。三层计费直接用量之和等于根用量；未知调用预留跨硬退出保留且必须核验。真实取消/超时证明普通命令树的停止，HTTP 返回仍受传输超时及协作检查约束。
- 库/CLI 输入边界：`test_library_call_limits_reject_nonintegers_before_model_dispatch` 拒绝 bool/小数/NaN/Infinity，`test_invalid_resume_limit_preserves_checkpoint_and_events` 核对保存状态不变。`test_jsondata.py` 实际调用 CLI，证明配置重复字段、非有限值或过深嵌套在新建会话/provider 前拒绝；核验文件出错时未知工具/用量及事件不变，合法 BOM 文件正常核验。实际 read(max_bytes+1) 取代 stat 后无界读取，非法大小参数在 open 前拒绝。

## 此前未满足的条件（2026-10-05 用户已接受估算模式）

当前唯一已识别的未满足要求是紧致可信 provider 输入计数器：需在请求前覆盖最终完整消息和工具 schema，并具备服务方依据，能保证较小预授权额度下的上界。库协议已支持 `Model(token_counter=...)`，但没有获得可绑定当前 DeepSeek 请求的可信实现。当前显式 profile 使用官方总上下文容量作保守上界，不能支持小于该容量的预授权额度；这不是原始计数器要求的完整替代。

2026-10-04 再查 [DeepSeek Token Usage](https://api-docs.deepseek.com/quick_start/token_usage/) 与公开 API 文档，未找到覆盖完整 chat/tool 请求的精确请求前计数保证；实际 usage 仍以模型返回为准。已向用户询问是否有服务方确认的接口/计数器说明。未获得该依据前，不用字符倍率或任意安全系数冒充可信上界，也不宣告整体目标完成。其余当前机械能力的审查结果限定于上述证据范围；旧真实报告继续作为历史证据。

后续补查 [Lists Models](https://api-docs.deepseek.com/api/list-models/) 的公开 API 导航及其链接的 [Anthropic 兼容指南](https://api-docs.deepseek.com/guides/anthropic_api/)：元数据仍只声明总上下文容量及输出上限；兼容指南的公开 HTML 未包含 count_tokens / token count / counting 入口说明。网页工具读取指南超时后，用无凭据公开 GET 成功读取并核对文本，未向未知计数端点发请求、未新增模型调用。这个结果仅说明已查文档没有给出所需契约，不证明服务内部绝不存在计数接口；剩余接入依赖服务方接口说明或用户提供的可信计数器依据。

### 2026-10-05 官方离线分词器包复核

重新读取官方 Token Usage 页，其下载链接当前指向 `https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip`。无凭据公开 GET 得到 1911504 bytes，SHA-256 `e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495`。仅在内存中读取 ZIP 目录及配置文本，未执行包内 Python、未安装依赖、未调用模型。包内有 `tokenizer.json`、`tokenizer_config.json` 和示例 Python；配置的 chat_template 处理 messages 中的角色、正文、历史 tool_calls 与工具结果，但没有处理请求 tools 参数中的工具 schema。配置 model_max_length 为 16384，不能据此替换已绑定的线上总上下文容量或推断线上模型版本匹配。

该包提供可用于估算的本地分词资料；现有文档与包内模板仍不能证明完整线上请求的输入 token 上界。尤其不能把正文分词结果、工具 schema 的 JSON 分词结果或人为安全系数当成 provider 保证。是否新增允许最后一次请求超额的估算预算模式，已向用户单独确认；严格预授权验收条件保持未满足。此复核未改变运行源码，此前测试和 wheel 的证据范围保持原样。

## 2026-10-05 估算预算模式验收

用户明确回复“行，可以超预算”，当前要求改为请求前本地估算、预留可配置余量和输出上限、返回后实际结算，允许最后一次请求超额并停止后续派发。严格预授权仍保留，不将估算标为服务方可靠上界；此前严格计数器依赖不再阻断这一获授权方案。

实现：`llm/estimation.py` 加载本地 tokenizer JSON，不执行远程代码，禁用自带截断/填充；完整最终消息和 tools schema 以规范 JSON 分词。`TokenEstimate` 与严格 reservation 明确区分；`agent/scope.py` 保存祖先预留/实际结算和误差，估算误差不触发严格上界违例；根及节点人工用量核验清理对应估算预留，仍禁止未知用量下继续调用。CLI 恢复契约绑定 tokenizer 内容摘要、依赖版本、余量和输出 cap。下载脚本固定官方 ZIP 摘要并只读取 tokenizer 数据。

专项 11 项通过：低估但未耗尽、实际超额持久化/增加预算不重发、估算余额不足无派发、未知量保留/超过估值核验、严格模式拒绝估算、子祖先记账和节点核验、禁用截断/填充及 tools 定义覆盖、CLI 无额度不创建 provider、SDK 输出 cap/请求摘要、缓存恢复不需要 tokenizer 或 provider。JSON 分词不是服务端聊天序列化；20%+256 余量不保证不会少算；error_tokens 比较总预留与实际总量，包含输出预留差异。

运行源码摘要 `df16caa91e72aa272045a432fdafdcc0f15152ffad901819ab7947ac4e905304`。wheel `.baseagent/estimated-package-audit/baseagent-0.1.0-py3-none-any.whl` SHA-256 `eeceed06d58fe87e2e6a54b075c26cb10882cab683293db35cba6b564c64758b`；解压后全部 runtime Python 文件字节与 checkout 相同，隔离解释器实际从 wheel 导入后六场景离线评估通过。同目录 report.json 和 wheel-evaluation.json 保存证据；复用本地依赖，未外部发布。

真实报告 `.baseagent/estimated-smoke-04e510bb89074157a5ffc3809082dcdb/report.json`：8 检查通过，1 次 DeepSeek Flash 请求、0 工具，精确回答 ESTIMATE_OK；总预留 4923、实际报告 2631 tokens，未知量 0、预留释放、误差 -2292 持久保存。真实场景证明请求/结算路径；超额和故障恢复由确定性专项验证。

当前源码严格预授权真实委派：`.baseagent/delegation-smoke-42295700e35c480187959785767ad2bf/report.json`，13 检查全部通过，4 模型/2 工具，实际报告 7323 tokens、未知量 0，整树预留释放、缓存恢复不创建 adapter/网络。

最终当前源码全量：`.venv/Scripts/python.exe -m unittest discover -s tests -q`，358 项，178.498 秒，退出码 0。先前一次全量在新增测试遗漏 --resume 参数时失败；修正测试入口后专项 11 项通过，并重新执行上述最终全量。运行源码在当前 wheel/真实验证与最终全量之间未变。整体功能映射及子任务十项契约由现有直接断言重新覆盖；用户明确接受估算超额后，当前计划约定的未决门槛已解除，未将估算证明为可靠上界。

## 后续改造：统一 backend 接口（2026-10-05）

用户要求先抽出 backend 接口。本轮新增 `backends/protocol.py` 和 `backends/local.py`：统一命名空间路径检查、字节读取、哈希、AGENTS.md、搜索、原子写入和 argv 命令执行。Workspace 保留权限与工作流，默认本机行为不变。LocalBackend 重用原生 CAS/锁/原子发布和 Windows Job/超时/取消实现；backend 的 contract 加入工具恢复摘要，环境身份变化在副作用前拒绝。原生指令遍历仍是 LocalBackend 内部实现，其他调用均经 backend 获取指令及哈希。

新增 test_backends.py 四项通过：无宿主目录的内存实现，并禁止 Path.open/os.walk/Popen，验证读写、CRLF 编辑、搜索、指令、真实工具验证记录、完成证据及外部变化拒绝；权限/保护路径拒绝且 backend 无副作用；环境身份变化恢复拒绝且 checkpoint 不变；本机 CAS 不覆盖及 argv 原样转交。现有 coding 21 项、completion 14 项、command deadline 5 项通过。最终全量 362 项通过（178.109 秒，退出码 0）；wheel 六场景离线评估通过，全部 runtime Python 文件与 checkout 字节一致。

这是 backend 接口与本机实现的改造，不新增 WSL/Docker 沙箱；LocalBackend contract 明确 isolation=none。未来远端 backend 必须在目标环境实现原子检查/发布、链接与路径限制、命令期限/取消及后代清理，不能将协议符合性作为隔离证明。新工具恢复契约会拒绝旧配置，需检查后显式接受配置变更。

Backend 改造当前源码摘要 `9509e0e3af2716e009576dcfeff08dfdf5db07a2ecc4a663e4972c517b0e0fba`。产物 `.baseagent/backend-package-audit/baseagent-0.1.0-py3-none-any.whl` SHA-256 `5b0d593e252fc3ebf980733cca16d4a21aa7746f77e6c8635f397abcdafa5b28`；隔离解释器实际导入解压 wheel，六场景通过，report.json/wheel-evaluation.json 保存证据。git diff --check 通过。此前真实模型报告保留其生成时源码边界；本轮没有新增付费模型调用。

提交前格式检查：移除 backends/local.py 文件末尾多余空行，无逻辑变化。上轮测试与 wheel 报告对应此格式修正前的源码字节；提交前 staged diff --check 再检查。
