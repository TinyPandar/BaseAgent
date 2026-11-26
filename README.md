# BaseAgent

白盒 agent 框架：单任务内核、可回放的工具账本、审批与预算，附带一个带引用的代码 RAG 应用（Project Assistant）。
目标是让一个编码 agent 的每一步都能被检查、回放和计量。

不需要密钥、费用 0 就能看全貌：

- 三分钟演示：[docs/demo.md](docs/demo.md)
- 离线总评测：`python scripts/evaluate_suite.py offline`
- 离线 harness 验收：`uv run baseagent --eval-harness`

<!-- BAS-2 side-dialogue: BEGIN -->
## 施工中 / 本分支需求：`/side` 独立侧聊（BAS-2）

> **状态：方案 A 已在本分支实现，离线验证；未合入 master，未做真实供应商验证。** 2026-11-26 按 Frank 选定的方案 A（续写主任务最近一次真实请求的前缀，未完成的工具改写成标注过的事实）以及与 job-wait 对齐后的共用规则实现：默认看主会话；`/side w1` 未获批准，没有实现；对话只在内存；沿用现有未知用量规则。用法、规则和证据见 [docs/side.md](docs/side.md)，下方「实现与验收（2026-11-26）」逐条列出 AC 证据与未验证项。下面的需求原文保留不改。
>
> 2026-11-25：根据 Frank 的侧聊讨论更新本段，替代原来“历史范围未定、仅需求占位”的说明。复用原 `plan/side-dialogue`，不另开重复分支。
>
> **前置已满足：插话收尾实现已合入 master，本分支已同步基线。** 2026-11-25 核对并承接 `origin/master` `3607848366aac95ee84d9b166b659b2d47e15916`，包含 [PR #3](https://github.com/TinyPandar/BaseAgent/pull/3) 原 head `abb8deebdde2c516ffc8d2f925fa7f561fef6088` 的实现。可以进入方案设计；侧聊仍未实现，须 Claude 先交至少两套结构不同的方案，Frank 决定后再实现。本次未启动实现者、未开发侧聊；下方原有“已交付”描述更新后的 master。

### 为什么做：场景、价值与取舍

起点是学习 fork 上下文 / subagent 的技术探索，并用一个 coding 中能直接体验的场景验证：主任务执行期间，用户可以另问“这个工具用了什么参数”“刚读的代码是什么意思”“这次修改依据是什么”，保留难用短摘要表达的细节，也帮助用户发现主线问题。不虚构真实用户规模或已经证明的性能收益。

与空白会话相比，侧聊减少用户重新补背景的负担；与临时调用模型生成摘要相比，fork 有机会避免额外摘要调用和细节损耗。但已有可用摘要、历史很长或必须大幅裁剪时，fork 未必更划算。默认同主会话模型，尽可能复用最近实际用过的稳定请求前缀；主会话活跃时更有复用机会。这里把缓存当值得验证的价值，不把 fork 或同模型本身当命中证明，也不为缓存牺牲权限或事实正确性。

本需求延续 [ROADMAP 的侧边对话范围](ROADMAP.md#内部消息类型与子-agent-并行)，吸收本轮关于进行中上下文、可见连续问答和流式体验的讨论；未在本段扩展的其他路线图事项不随之开工。

### 正常使用流程

1. **打开快照：** 用户在主任务运行或空闲时显式发起 `/side` 并提出问题；形成创建时的上下文快照，说明来源、时点及缺失，主任务照常推进。进行中的工具及已知参数/进度不能仅因当前轮未结束就全部丢掉。
2. **侧边问答：** 以用户可见的独立对话回答，而不是隐藏 Worker 只交一份总结；提供明确归属的状态与渐进显示。只据可见上下文回答，不执行工具、不改变主任务。
3. **继续追问：** 后续问题沿用创建时快照和该侧聊自己的对话，不自动吸收主任务后续消息，也不把侧聊问题/答案送回主线。
4. **结束侧聊：** 能明确停止或结束侧聊，不误停主任务、Worker 或后台命令；关闭视图、断线、重连和显式重试有可理解的后果，已发生或状态不明的调用/费用不能被抹去。

### 首版范围、要求与产品假设

**明确要求：** 显式触发；默认同主会话模型；创建时当前可见上下文，含当时已公开的进行中事实；用户可见的独立连续对话；无工具、答案不回写；独立计量并遵守适用总额限制。低成本缓存与流式快答是体验目标，需要分别给出正确性证据和性能证据，不能混为一谈。

**本版采用、需在方案中显式说明的产品假设：**

- 上下文视野默认取创建时主会话当前可见的上下文及当时已产生、可向用户展示的进度事实，受现有上下文/预算约束，不是无界复制全部历史；不是只退回最后一个完整轮而丢弃当前进行中事实。主会话尚未获得的 Worker 内部历史、隐藏推理或工作区实时状态不自动变成侧聊已知信息。方案须用示例说明压缩、裁剪与细节保留的取舍，不擅自加入任意选历史范围的新产品界面。
- 创建后固定快照，支持侧聊自己的多轮追问；不做实时追新、显式刷新或多侧聊管理。这是当前范围假设，不把此前助手的推荐包装成 Frank 对每个细节逐项拍板；若方案发现核心场景因此无法满足，先向 Frank 提出冲突，不自行改成自动追新。
- 默认继承主会话模型及兼容的生成参数；允许为侧聊未来回答独立降低受支持的思考预算以争取更快响应，不偷偷换便宜模型，不修改主任务参数。不支持时明确说明，不承诺各 provider 都有相同旋钮；减少未来思考不等于删改历史 reasoning。
- CLI/TUI/Web 保持等价的触发、路由、隔离与计量语义。能承载侧栏的 TUI/Web 界面提供与主窗体验一致、清楚分离的侧边对话区；CLI 提供可辨认的等价对话呈现，不虚构图形侧栏。逐步到达的回答是本期体验目标；不支持流式的 provider 可明确降级为完整回答，不能谎称已流式。具体入口和呈现由方案比较，不指定类、模块或消息接口。
- 明确“停止/结束侧聊”仅针对侧聊；前端临时断线不是重新提交问题。关闭视图时须让用户知道是仅隐藏仍在运行，还是结束侧聊，不能静默继续计费而不提示。具体交互须在方案中讲清，与技术方案一并请 Frank 确认。

**首版不做：** 文件/命令/MCP/网络/委派等任何侧聊工具执行，即使标为只读；工具权限升级和沿用主任务批准；新的引用定位系统；自动采纳、合并、steer/tell/queue 主线；用户可选模型的新路由系统；读取或展示未对外提供的内部推理；实时刷新、多侧聊管理、持久化多分叉产品；Worker `fork_turns: none/N/all` 重构、单 Agent 默认调整、worktree 并行写、job_wait/job_input、Docker 后台或新的 `/jobs`/`/kill`。工具及升级权限、引用定位是后续讨论，不因这次讨论自动进入首版。旧 Chainlit 不扩展。

### Done：沿正常路径验收与故障守约

#### 一、打开快照

- **AC01｜显式发起、路由不混。** CLI/TUI/Web 都能显式进入侧聊；未发起时不额外调用模型。普通主线输入、`/queue`、`/tell`、审批回复仍走原有目标和语义；主任务运行时打开侧聊不暂停、取消或重启原命令，不重复命令副作用。
- **AC02｜当前信息不被完整轮边界抹掉。** 受控主任务已读过一段代码、正在以已知参数执行长工具调用时，立即发起侧聊，侧聊输入同时包含选入的已知细节与“正在执行/结果未知”的事实，而不是等待原工具结束或只拿上一完整轮。显示来源/快照时点；未选入、被裁剪或尚不可见的内容不能伪称已知，侧聊回答不能声称工具已完成。
- **AC03｜投影合法、并发一致、主历史不变。** 单工具/并行批次尚未结束、工具部分返回、压缩或恢复与创建相邻时，侧聊模型请求均满足消息/工具配对与 provider 历史约束，不串入创建后才出现的结果，不伪造真实成功，不混接不同版本的摘要与尾部。主真实历史、账本及待处理调用不被侧聊修补或改写；带签名思考块遵守 provider 保留规则。占位符只是候选，合法投影方式由方案决定。
- **AC04｜默认模型与前缀价值可解释。** 假模型/捕获请求证明默认 provider/model 与主会话一致；对照最近实际主请求与侧聊首请求，展示可复用的稳定前缀及首次差异，解释 system、工具描述、历史投影、参数差异的原因，不为命中保留执行权、伪造历史或修改主配置。缓存 miss 时仍正确问答；离线请求对比不当成真实 cache 命中或省钱证明。

#### 二、侧边问答

- **AC05｜可见独立对话、三前端兼容。** 用户能直接看到自己的侧聊问题、回答及状态，与主线/Worker 消息清楚区分；TUI/Web 的侧边显示与主窗具有一致的基本问答体验，CLI 有语义等价呈现。主线同时输出时不串入侧聊、输入焦点和输入目标可辨认；不以后台 Worker 最终总结替代用户可追问的侧聊。
- **AC06｜流式是到达过程，不是事后动画。** 对支持流式的路径，用受控分块模型证明生成结束前已能看到回答逐步到达，最后完整文本与分块一致；主/侧事件不串线。对不支持的路径，明确显示能力限制并正常完整回答，不假装流式、不悄悄改主线 stream 设置。中途失败/停止显示已有部分及未完成状态，不标成成功终稿。当前默认连续对话的流式缺口必须纳入方案影响面，不能只凭底层库支持流式就宣布此项完成。
- **AC07｜无工具是执行边界，不是提示词愿望。** 假模型尝试读写文件、执行命令、调用 MCP/网络工具、派生 Worker，或借历史工具指令/主任务审批要求执行时，都不能产生相应执行/工作区业务文件变化。侧聊自身对话、显示与计量记录不算业务工具操作。历史/检索/工具文本中的指令只作参考，不成为新授权；“只读”工具也不默认开放。不能为缓存放宽这一边界。
- **AC08｜未来思考参数隔离。** 验证默认继承兼容参数；受支持时侧聊可独立降低未来回答的思考预算，主任务前后参数不变，历史带签名内容不被删改。无此能力或参数不兼容时给出清楚结果/限制，不静默切模型、抬高费用上限或声称低预算必然更快；不暴露未对外提供的推理内容。

#### 三、继续追问

- **AC09｜连续问答但不自动追新。** 主任务在侧聊创建后取得新结果或修改文件，侧聊后续问题仍基于原快照和自己的问答历史；能延续侧聊上一问，不每次重新 fork、丢失侧聊上下文或暗中带入新主线消息。固定对话快照不等于工作区环境快照，无法知道的当前状态应如实说明。
- **AC10｜不回写、不接管。** 含“建议修改/请执行”等内容的侧聊问题和答案，不自动写入主线历史、收件箱或 Worker 指令，不成为 steer/tell/queue 或新的主任务，不自动采纳。除主任务自然推进及明确归属侧聊的显示/计量外，主线目标、权限和工作状态不受影响。
- **AC11｜裁剪与失败不伪装语义完整。** 以较小上下文限额、已有摘要、缺失内容及 provider 错误验证：按披露的范围合法裁剪或明确无法回答，不能用非法工具历史勉强发请求，不把缺失事实编成已知，也不暗中增加一次摘要模型调用。后续追问触及缺失内容时诚实说明；具体裁剪策略及成本取舍由方案论证，不在本需求指定算法。

#### 四、结束与异常收尾

- **AC12｜停止对象与主线隔离。** 停止/结束操作有明确目标；侧聊取消不误停主任务、Worker 或后台进程，主任务正常结束不自动把仍在回答的侧聊标成失败。侧聊结束后不自行发起后续调用；关闭视图时运行/结束的后果可知，主线输入和停止语义不退化。
- **AC13｜断线、重连、重试与取消诚实。** 在分块输出、请求刚发出、回答将完成等受控时点停止或断连，重连/事件重放/重复提交同一次操作不会偷偷新发一次生成、重复展示终稿或重复触发副作用。用户明确重试按新尝试记录并累计用量；前次是否已被 provider 接收、是否取消及费用不明时，不伪报未调用、零费用或已成功取消。只恢复本来能恢复的状态，不借此扩展成持久化多分叉管理。
- **AC14｜分项计量与适用总额同时成立。** 可核对的 fake usage 覆盖主线/侧聊并发、侧聊多轮、失败、重试及现有续跑/恢复入口：侧聊用量可独立识别，既有累计用量不重置，适用的共享/总额护栏不被同时开聊绕过；侧聊自身超限/失败不直接取消主任务，不提高主预算。不明用量保留未知并遵守既有拒绝继续规则，不伪填 0；CNY 是软上限，在途请求可能超支。
- **AC15｜竞争窗口用受控事实验证。** 用本地同步点控制真实并发执行的开始/阻塞/完成，覆盖创建与并行工具返回/压缩相邻、主侧同时输出、停止与完成竞争；验证快照一致、消息配对完整、命令只启动一次且 PID/副作用不因侧聊变化、侧聊错误不泄露到主任务生命周期。复用已有命令/Inbox/权限/恢复能力，不重做它们，不靠长 sleep 或“没有报错”代替证据。
- **AC16｜证据分层且不虚报。** 更新基线后运行针对上述路径的聚焦及全量离线测试，隔离真实 `.env`、MCP 信任配置及密钥，记录 refs、用例到 AC 的映射、通过/失败/skip，并对失败按需比较基线。分别列出请求契约、真实受控并发、前端自动化、人工终端/浏览器、真实 provider/cache 性能的证据或未验证项；假模型/界面模拟不冒充真机/API/缓存实测。需求阶段本次不运行无关全量或付费验证。

### 怎么验证、哪些证据不等价

| 层次 | 本期应给的证据 | 成本与边界 |
| --- | --- | --- |
| 假模型 / 请求契约 | 当前事实和合法投影、模型/参数继承、前缀差异、无工具边界、用量账目、多轮上下文；对应 AC01–AC04、AC07–AC11、AC14 | 零模型 API 费用；不能证明缓存命中、真实推理质量或取消成功 |
| 真实受控并发状态 | 用同步点和临时本地命令验证进程身份/单次副作用、创建/工具批次/压缩/取消/完成竞争；对应 AC01–AC03、AC12–AC15 | 零模型 API 费用；不运行真实任务、不下载大型资源、不碰真实凭据 |
| 前端自动化与人工体验 | CLI/TUI/Web 触发、目标归属、渐进输出、断连/重连和停止；对应 AC01、AC05–AC06、AC12–AC13；人工体验另列 | 自动化可零模型费用；模拟不等于人工真实终端/浏览器已验证 |
| 真实 provider / 缓存性能 | 如获授权，对相同背景的空白补背景、可用摘要、fork 请求核对实际 cache/read tokens、usage、首字/完成延迟及费用；记录模型/参数/上下文差异 | 不属于本次授权；另向 Frank 申请明确预算。无实测不写命中率、提速或省钱数字；已有摘要时不强行新增摘要调用 |

### 实现与验收（2026-11-26）

实现：`src/baseagent/agent/side.py`（快照、请求构造、无工具的侧聊状态机，不引用工具注册表、工作区、MCP、Worker 或 agent 循环）。改动的现有代码都是接点：`run_agent` 的被动请求观察回调、`SessionStore` 的一致读与首次用 `/side` 才建的 `side_attempts` 表、`TurnBudget` 的侧聊准入与结算、OpenAI 兼容适配器的可关闭流和可选保留 `reasoning_content`、`ChatSession` 与三个前端。kernel 没有改。默认行为不变：不输入 `/side` 就不发侧聊请求，也不建表。

| AC | 证据（全部离线、假模型或本机假供应商，零 API 费用） |
| --- | --- |
| 01 | 三个前端都能显式进入；不用 `/side` 时没有侧聊请求、不建表；`/queue`、`/tell`、Worker 归属不变；审批等待中输入 `/side` 不会被当成审批回答；主任务运行时打开侧聊，命令只启动一次（`test_side_frontends`、`test_side_snapshot`、`test_side_providers`） |
| 02、03 | 请求在途、工具部分返回、并行批次半返回、压缩相邻、读取期间又有请求返回、历史被改写：快照包含当时已知的事实，不混版本，消息配对合法，主历史、账本和收件箱逐字不变（`test_side_snapshot`） |
| 04 | 同一次主请求的会话历史、最终投影、实际线上请求与侧聊首请求逐层对照，给出首个差异和共享前缀：Agent 层的投影不等于 Anthropic 的线上格式，因此比对在线上请求上做（`test_side_providers`） |
| 05、06 | CLI 前缀行、TUI 侧栏、Web 侧聊面板与独立 SSE；受控分块证明回答在结束前逐段到达；Anthropic 明确提示“不支持流式”（`test_side_frontends`、`test_side_providers`） |
| 07 | 模型在侧聊里请求 `run_command`、`write_file`、`delegate_task` 时：没有执行、没有文件变化、没有新 Worker、账本不变；导入边界测试（`test_side_frontends`、`test_side_snapshot`） |
| 08 | 默认沿用主会话的模型和参数；本版不提供单独降低思考预算，打开时明确说明；签名思考块原样回传，假供应商按前缀规则接受（`test_side_providers`） |
| 09、10 | 追问复用同一快照，任务参考只加一次；主任务之后的新结果不进侧聊，侧聊内容不进主线、收件箱或 Worker（`test_side_snapshot`、`test_side_frontends`） |
| 11 | cold、裁剪（注明省略条数）、模型请求工具后继续追问，在三种供应商规则下都合法；破坏规则的反例被拒（`test_side_providers`） |
| 12、13 | 停止、结束只作用于侧聊，主回合与 Worker 继续；停止与完成两种先后各结算一次；晚到用量不显示正文但照样记账；重复 `op_id`、重连、显式重试不重复生成（`test_side_accounting`、`test_side_frontends`） |
| 14 | 跨主轮次时用量归受理时的那一轮；未知用量不按 0 计，设了 `--budget-cny` 时按现有规则拦住这一轮；崩溃重启后侧聊结束、用量和未知次数保留并提示，从库里重建的花费与运行中一致（`test_side_accounting`） |
| 15 | 所有竞争都用事件同步点和真实本地子进程控制，不靠 sleep 赌时序 |
| 16 | 见本段下方的“验证记录”与 docs/side.md 的证据表；真实供应商、缓存、延迟、费用和人工体验**未验证** |

**验证记录（2026-11-26，Windows 10，Python 3.12.13，基线 `887c061`）**：测试环境建在仓库外，隔离真实 `.env` 和 MCP 信任配置，不用密钥，不调用付费 API。

- 侧聊测试 39 项全部通过（`tests/test_side_snapshot.py`、`test_side_providers.py`、`test_side_accounting.py`、`test_side_frontends.py`），连跑 3 遍都通过；时序全部用事件同步点和真实本地子进程控制，没有靠重试。
- 变异检查：每次只破坏一条侧聊规则（共 21 条：例如未完成批次原样发送、侧聊说明改成 system 消息、不复核最近请求、任务参考取走收件箱或后台任务通知、晚到用量丢弃或记到当前轮、结算不止一次、重复 `op_id` 被受理、侧聊占用主任务调用次数、审批等待中 `/side` 被当成回答），测试全部失败。
- 全量离线回归：改动前 1425 通过 / 97 失败 / 24 错误 / 21 跳过；改动后 1465 通过 / 96 失败 / 24 错误 / 21 跳过。**没有新增失败**：改动后的失败在改动前都已失败，原因是这台 Windows 机器的环境（找不到 git、docker 等可执行文件，文件被占用，没有创建符号链接的权限，没有 ripgrep）。改动前失败、改动后通过的有 1 条（`test_checkpoint_chat` 的 TUI 撤销），没有归因。检索语料版本没有重绑，`corpus_version` 相关测试本来就失败。
- 现有测试只改了 1 处：`test_kernel_compatibility` 的新增参数登记表里加上 `request_observer`（仅限关键字、默认 `None`，检查强度不变）。
- **未验证**：真实供应商（OpenAI 兼容、DeepSeek、Anthropic）、真实缓存命中、首字延迟和费用；人工在真实终端或浏览器里的体验；macOS / Linux。

已知限制：主线现有的一个问题（侧聊不会新增它）——走 Anthropic 时，工具结果超过 `--max-tool-context-bytes` 会让 `ContextPolicy` 插入一条 system 提示，并入顶层 `system` 后签名思考块不再匹配，详见 docs/side.md。

### 依赖、现状与给实现者的交接

- **依赖：** 插话收尾实现已合入 master，本分支通过普通合并同步至上述基线，未改写已推历史。侧聊可进入方案设计，Frank 选定方案后再实现；`job_wait` 不是侧聊的语义前置，二者可分别设计，但不承诺无代码冲突或免集成验收，不把 job_wait 顺带实现。
- **现状核实：** 已有上下文投影、消息配对、会话事件、审批/预算等基础能力，应复用其已验证契约；不代表 fork、侧聊多轮、分项预算或侧栏流式已经接好。[当前 UI 文档](docs/ui.md#实现时自己拍板的点) 与 `src/baseagent/main.py` 的 `LazyModel` 显示：默认连续对话尚无贯通的逐 token 流式接口，Web SSE 是事件流，不等于模型 token 流。流式体验涉及的影响面必须估清，不装成零成本小接线；不能以此偷偷改主任务流式配置或历史协议。
- **Claude 先交方案：** 围绕目标与 AC，用 pstack architect 给至少两套结构不同的方案及取舍，说明快照新鲜度/合法性、压缩裁剪、稳定前缀/无执行权、可见多轮对话、流式跨前端缺口、参数和预算隔离、生命周期/失败恢复的成本与影响面。Frank 决定后才进入实现；本段不指定新 Agent 类、模块、接口、底层执行器或占位消息形状。方案若与产品假设冲突，先回报，不自行重定范围。
- **授权与费用：** 侧聊自身需求仍只改 README；本次基线同步仅承接 master 已验收的插话实现，不新增侧聊业务代码。普通提交并推送本需求分支，不授权启动侧聊实现者、侧聊业务实现、侧聊合入或付费。离线验收模型 API 费用为 0；真实模型/缓存/流式/API 验证与跑分须 Frank 另行授权并带预算。需求文档变更不重绑语料版本，留实现/合入阶段按既有流程处理。本次不搭 CI 平台、不重写权限系统、不改变产品默认模式。
<!-- BAS-2 side-dialogue: END -->

## 状态

### 已交付（master，离线测试覆盖）

| 能力 | 要点 | 文档 |
| --- | --- | --- |
| 单任务内核与持久会话 | SQLite 检查点、工具账本（`pending → running → completed`）、中断后保守恢复与人工对账、步数 / 模型调用 / 工具调用 / token / 时长预算 | [参考手册](docs/reference.md) |
| 一次性任务与连续对话 | `baseagent "task"` 一次性；不带任务进入连续对话；`/status`、`/continue [N]`、`Ctrl+C` 只取消当前轮 | 下文「快速上手」 |
| 插话与统一收件箱 | 主 Agent 回复中可以继续输入：普通一行在下一次模型调用前送达本轮（只在整批工具调用都有结果之后），`/queue <text>` 排到下一轮；本轮结束时没送达的自动作为下一轮，输入不会丢。开启 `--background-jobs` 的适用本地 `run_command` 在本 Agent 收到用户 steer、命令已运行 1 秒后，可将同一个进程提前交给后台，不等原超时；整批工具结果齐备后才送达插话，不重启命令。`/queue`、`/tell` 与其他来源通知不触发此交接；普通主线输入不自动转向 Worker。Worker 结果、`/tell`、后台任务通知走同一个收件箱；未送达的内容随会话保存。终端 CLI 在后台线程跑回合、继续读输入，审批时下一行就是回答（`y` 允许；`n`/回车拒绝；其它文字拒绝并作为插话交给 Agent）。管道输入保持逐行顺序执行 | [docs/inbox.md](docs/inbox.md) |
| 主 Agent + 后台 Worker | 连续对话默认开启；主 Agent 只对话和分派，Worker 在后台跑，结果自动推送；`--max-workers` 默认 2，同时只允许一个写入 Worker；`--no-main-agent` 回到单 Agent。主 Agent 和当轮 Worker 共用步数、模型调用和可选的 `--budget-cny`；续跑不清零，恢复不出已用额度则拒绝。CNY 是软上限，在途请求可能超支。 | [docs/router_worker.md](docs/router_worker.md#预算共享) |
| 三个对话前端 | `--ui cli\|tui\|web`，共用同一层会话和事件；Web 一个进程一个会话 | [docs/ui.md](docs/ui.md) |
| 权限与审批 | `--mode read-only\|edit\|full`；`full` 下命令类工具默认先询问，`--auto-approve` 关闭；`--tool-policy` 只能再收紧；审批绑定单次调用 | 下文「权限与审批」 |
| 命令级规则 | `--tool-policy` 条件规则支持 `argv_prefix`（如允许 `git status`、询问 `git push`、拒绝 `rm`）；可选 `"precedence": "strictest"`（deny > ask > allow）；放行 shell / 解释器前缀会告警。前缀规则只是弱黑名单，建议默认 `ask` / `deny` | [docs/command_rules.md](docs/command_rules.md) |
| 工具属性与关键路径保护 | 每个工具标注 `read_only` / `destructive` / `idempotent` / `open_world`，`--tool-policy` 可按属性设 `ask` / `deny`（如所有破坏性工具都要询问）；MCP 服务器自带的提示不放宽权限。内置的关键路径保护始终生效，拒绝删除磁盘根、工作区根、家目录、`.git`、`.baseagent` 的命令（argv 层面的尽力检查，不是沙箱） | [docs/tool_annotations.md](docs/tool_annotations.md) |
| OS 沙箱 | `--sandbox read-only\|workspace-write`（默认 `off`）：命令只能写工作区和临时目录，保护路径只读，默认断网（`--sandbox-network` 打开）；macOS `sandbox-exec`、Linux `bwrap`，**只做了单元测试，未在真机验证**；原生 Windows 不支持。不可用时退出码 3，不会退回不受限执行 | [docs/sandbox.md](docs/sandbox.md) |
| 后台任务 | `--background-jobs`（默认关闭，需 `--allow-command`）增加 `start_job` / `job_list` / `job_output` / `job_kill`；任务不随轮次结束或取消而终止；`run_command` 超时转为后台任务而不是被杀；重启后可查到以前的任务（防 PID 复用）；`keep_running=true` 的任务在退出后继续运行；可在 OS 沙箱里运行；仅 LocalBackend | [docs/background_jobs.md](docs/background_jobs.md) |
| 检查点与撤销 | 每轮一个文件快照；`/checkpoints`、`/undo`、`/rewind N`；只撤文件工具的写入，撤不回命令 | [docs/checkpoints.md](docs/checkpoints.md) |
| 多文件原子补丁 | `apply_patch` 一次最多 50 个文件，全有或全无，失败按快照回滚 | [docs/patch.md](docs/patch.md) |
| 机器可读输出 | 一次性任务和 `--resume` 支持 `--output-format json\|jsonl`，`schema_version` 1 | [docs/output.md](docs/output.md) |
| 项目规则文件 | 根目录 `AGENTS.md` 作为参考数据交给模型；单文件 20 KB、合计 40 KB，超出截断；不读 `CLAUDE.md`；`--no-project-rules` 关闭 | [参考手册](docs/reference.md) |
| MCP 客户端 | 只支持 stdio 和工具调用（`initialize` / `tools/list` / `tools/call`），默认关闭；有副作用的 MCP 工具每次审批；项目 `.mcp.json` 首次使用要确认信任 | [docs/mcp.md](docs/mcp.md) |
| 上下文管理 | 超大工具结果溢出到会话文件（默认开）；接近窗口上限时确定性摘要旧轮次，不调用模型 | [参考手册](docs/reference.md) |
| Watchdog | 任务级停滞 / 重复 / 预算 / 超时巡检，默认关闭 | [docs/watchdog.md](docs/watchdog.md) |
| HarnessKnight 适配层 | 可选的外部安全审查服务，默认关闭 | 下文「安全边界」 |
| Docker 沙箱 | CLI `--backend docker`：一次运行一个容器（对话的各轮和 Worker 共用），命令和文件读写都在容器里；默认断网，工作区挂到 `/workspace`。Docker 起不来时调用模型前退出码 3，不会退回 local。默认仍是 LocalBackend，它不是沙箱。离线测试用假 Docker，真实冒烟尚未跑。 | [docs/docker_backend.md](docs/docker_backend.md) |
| Harbor / Terminal-Bench 官方接入 | Harbor 0.26.0 外部 agent + ExecBackend；容器生命周期与评分由 Harbor 管理；固定 18 题 id。假环境与离线故障回归已验，真 Docker fixture、完整 dry-run 和真实成绩仍未验证。 | [docs/terminal_bench.md](docs/terminal_bench.md) |
| Project Assistant（代码 RAG） | 文档 / 代码入库、带 `path:start-end` 引用的检索；默认 vector + BM25 混合检索，rerank 默认关闭 | [应用说明](applications/project_assistant/README.md) |

### 只有离线管道，没有真实结果

| 项目 | 已有 | 没有 |
| --- | --- | --- |
| 流式输出、并行只读工具 | `--stream`、`--parallel-readonly-tools`，离线测试 | 付费冒烟 |
| Trigger（定时 / 文件变化唤醒 Worker） | 库 + `scripts/demo_triggers.py` | CLI 入口 |
| 生成答案质量 | 分层打分管道，用合成答案验证 | 真实模型 80 题结果 |
| Terminal-Bench | `--synthetic` 为合成报告；`--fixture` 走 Harbor；`--fixture --backend docker` 保留独立的本地 CLI 容器链路冒烟（默认假 Docker）。两条 fixture 的真 Docker 路径均未实测 | 真实子集成绩、真 Docker fixture 与完整 dry-run |

## 评测数据

| 项目 | 结果 | 样本与条件 | 来源 |
| --- | --- | --- | --- |
| 检索 hybrid MRR | 0.845（vector 0.678，BM25 0.759） | 80 题集的 test 划分，28 道可答题；参数在 tune 划分上选定；2026-10-21，当时的语料 | [报告](applications/project_assistant/evaluation/reports/bailian-80q-summary-2026-10-21.md) |
| 受控 coding 任务成功率 | 改动前 2/3 → 改动后 5/5 | 同一个夹具任务；qwen-plus 录像，改动前 3 次、改动后 5 次；2026-11-02 | [报告](docs/reports/m1_coding_comparison-20261102T111829Z-b21f9bdd6a3745a4b572a5e3dcf309e1.md) |
| 该任务平均总 token | 80780 → 36185.4（−55.2%） | 同上；改动后平均约 0.030 元/次 | 同上 |
| 离线 harness 场景 | 6/6 | 脚本化模型，测的是框架机制，不是模型能力 | `baseagent --eval-harness` |

检索各模式（test 划分，只计可答题）：

| 模式 | R@1 | R@5 | MRR |
| --- | ---: | ---: | ---: |
| vector | 0.378 | 0.710 | 0.678 |
| BM25 | 0.407 | 0.734 | 0.759 |
| hybrid（默认） | 0.567 | 0.782 | 0.845 |
| hybrid + qwen3-rerank | 0.583 | 0.770 | 0.857 |

真实 80 题生成质量、Terminal-Bench 真实子集，以及流式 / 并行 / 主 Agent 对话的付费冒烟都还没跑。付费命令要单独授权并带 `--budget-cny` 上限，见 [docs/eval.md](docs/eval.md)。

## 失败与取舍

- rerank 默认关闭：MRR 只高约 0.012，R@5 反而从 0.782 降到 0.770，每次查询多约 1.4 万 token。
- MRR 只说明找到了证据，不说明答对了；生成质量只验证过打分管道。
- coding 数字来自一个受控夹具任务的少量录像，不代表通用编码能力。
- LocalBackend（默认）不是沙箱。需要隔离时用 `--backend docker`：默认断网，工作区挂到 `/workspace`；它是容器不是虚拟机（共享内核），工作区是 bind mount，命令对工作区的改动 `/undo` 撤不回，MCP 服务器仍在宿主上跑。Docker 起不来不会退回 local，退出码 3。真实 Docker 冒烟尚未跑，见 [docs/docker_backend.md](docs/docker_backend.md)。
- `/undo` 只撤文件工具的写入，撤不回命令的副作用。
- `--budget-cny` 是软上限：一次已放行的请求返回后可能把总额推过上限，这部分照样计费；`/continue` 和 `--resume` 不会清零或自动提高金额上限。
- Watchdog 的停滞规则只把写入和验证命令算作进展，只读任务可能被提前停止（见「安全边界」）。

## 快速上手

### 1. 安装

需要 Python `>=3.12`。推荐 [uv](https://docs.astral.sh/uv/)（仓库含 `uv.lock`）：

```bash
git clone https://github.com/TinyPandar/BaseAgent.git && cd BaseAgent
uv sync                     # 核心依赖
uv sync --extra frontends   # 可选：TUI + Web 两个对话前端（也可分别 --extra tui / --extra web）
uv sync --extra ui          # 可选：Chainlit 浏览器界面
```

也可用 venv：`python3 -m venv .venv && .venv/bin/python -m pip install -e .`。Windows 下把 `.venv/bin/python` 换成 `.\.venv\Scripts\python.exe`。

### 2. 配置模型

在仓库根目录建 `.env`（已被 `.gitignore` 忽略）：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
# DEEPSEEK_MODEL=deepseek-chat
```

也可以用 `BASEAGENT_LLM_BASE_URL`、`BASEAGENT_LLM_API_KEY`、`BASEAGENT_LLM_MODEL` 指定任意 OpenAI 兼容模型；设置任一 `BASEAGENT_LLM_*` 后不再混用 DEEPSEEK 凭据，`BASEAGENT_LLM_EXTRA_BODY` 可传 JSON 对象。

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` | 使用 DeepSeek 配置时必需 |
| `DEEPSEEK_MODEL` | 默认 `deepseek-chat`；`--model` 优先 |
| `OPENAI_API_KEY` | RAG embedding 凭据回退项（`RAG_EMBEDDING_API_KEY` 优先）；`--mock-embedding` 时不需要 |
| `RAG_EMBEDDING_PROVIDER` / `RAG_EMBEDDING_MODEL` | 默认 `openai` / `text-embedding-3-small` |
| `RAG_INDEX_DIR` / `RAG_COLLECTION_NAME` | 默认 `.baseagent/rag_index` / `baseagent_docs` |
| `BASEAGENT_CASSETTE_MODE` | 测试用，默认 `replay` |

CLI 和 `python -m applications.project_assistant` 会加载 `.env`；`scripts/build_knowledge_base.py`、`scripts/query_knowledge_base.py` 只读进程环境变量。

Anthropic：`--provider anthropic`（或配置 `run.provider`），只读 `ANTHROPIC_API_KEY`（必需）、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`（默认 `claude-opus-5-5`），不会混用其它供应商的凭据；`--thinking` / `--effort` 只对它生效，思考块随会话保存并原样回传；`--stream` 暂不支持。**目前只有离线测试，真实 API 尚未验证**（`scripts/anthropic_smoke.py --yes-spend-money`，需要密钥，最多 3 次请求）。见 [docs/providers.md](docs/providers.md)。

### 3. 不需要密钥的命令

```bash
uv run baseagent --list-tool-packages
uv run baseagent --eval-harness          # 六个脚本化场景，临时工作区里跑
python -B scripts/demo_offline.py        # 假模型走真实 harness：流式、审批、恢复
python -B scripts/demo_duplex.py         # 假模型演示主 Agent + 两个 Worker
```

### 4. 一次性任务

```bash
uv run baseagent "Explain the project structure" --root .                    # 默认只读
uv run baseagent "Fix the failing test and verify it" --root . --mode full   # 允许写文件和跑命令；命令先询问，一次性任务会停在第一条命令
uv run baseagent "Fix the failing test and verify it" --root . --mode full --auto-approve   # 命令不询问（以前的行为）
uv run baseagent "Explain the project structure" --root . --output-format json
uv run baseagent "Fix the failing test" --root . --mode full --auto-approve --backend docker
# 命令和文件读写在容器里，默认断网；先 docker pull python:3.12-slim
# Docker 不在或缺镜像时退出码 3，不会退回 local
```

默认上限：8 步、8 次模型调用、32 次工具调用。撞上限后用 `baseagent --session ID --resume --max-steps 32 --max-model-calls 32` 接着做，已用计数不清零。会话默认存在 `.baseagent/sessions.sqlite3`（`--db` 可改）。

### 5. 连续对话

```bash
uv run baseagent --root . --mode edit                          # 终端逐行对话
uv run --extra tui baseagent --ui tui --root . --session demo  # 终端界面
uv run --extra web baseagent --ui web --root . --session demo  # http://127.0.0.1:8765/，非本机地址必须带 --token
```

- 默认上限放宽到 16 步、16 次模型调用，剩 2 步时提示模型收尾；显式参数或 `--config` 优先。
- 撞上限时输入 `/continue [N]` 在对话里提高本轮步数和模型调用上限，已用计数不清零、已完成的工具调用不重做；`--budget-cny` 不会因此清零或提高。
- 常用命令：`/status`、`/wait`、`/cancel <id|all>`、`/tell <id> <text>`、`/checkpoints`、`/undo`、`/rewind N`、`/exit`；侧聊 `/side <问题>`、`/side-stop`、`/side-retry`、`/side-end`（本分支，见 [docs/side.md](docs/side.md)）。
- 默认是主 Agent 模式：主 Agent 不读写文件、不跑命令，需要动手时派出 Worker；Worker 默认只拿只读工具，用户明确要求修改时才允许写入。主 Agent 这一轮和它派出的 Worker 共用步数、模型调用和可选的 `--budget-cny`；续跑沿用已花金额。完整规则见 [参考手册](docs/reference.md#共享预算) 和 [预算共享](docs/router_worker.md#预算共享)。
- `--session` 可以在三个前端之间接着用；连续对话也可以加 `--backend docker`。

完整行为（Worker 推送、检查点、MCP 首次信任、`AGENTS.md`、模型重试、时间戳约定）见 [参考手册](docs/reference.md)。

### 6. Project Assistant 离线演示

```bash
.venv/bin/python -B scripts/demo_knowledge_search.py
.venv/bin/python -B scripts/build_knowledge_base.py --mock-embedding
.venv/bin/python -B scripts/query_knowledge_base.py "kernel architecture" --mock-embedding
```

mock embedding 只验证流程，不代表检索质量。

## 权限与审批

`--mode` 是现有权限标志的简写，用于一次性任务、`--resume` 和三个对话前端；不传时等同 `read-only`。

| `--mode` | 等价标志 | 模型能看到的写 / 命令工具 |
| --- | --- | --- |
| `read-only` | （默认） | 无；读、`grep` / `glob`、`git_diff`、`git_status`、计划和记忆工具照常可用 |
| `edit` | `--allow-write` | `write_file`、`edit_file`、`apply_patch` |
| `full` | `--allow-write --allow-command`，外加"命令先询问" | 再加 `run_command`、`verify_command`；**这些命令类工具（同时是 destructive 和 open_world 的工具，含 `start_job` 和非只读 MCP 工具）默认先询问**，文件工具、`job_kill` 仍自动执行；`--auto-approve` 恢复不询问。LocalBackend 下 stderr 警告命令不在沙箱里，`/undo` 撤不回命令造成的改动；`--backend docker` 下改为说明命令在容器里、网络是否开启 |

- 未开启的工具不出现在发给模型的工具列表里，硬调用也会返回 `permission_denied`。`publish_memory` / `withdraw_memory` 另由 `--allow-memory-publish` 控制。
- 显式的 `--allow-write` / `--no-allow-write` 等标志与 `--mode` 矛盾时报错（退出码 2）。
- **`--mode full` 的命令默认询问**（见上表），一次性任务停在审批时会打印可直接复制的批准命令，JSON 输出带 `approval_hint`；`--tool-policy` 里对某个命令工具显式 `allow`（或命中的 `argv_prefix` 允许规则）会免去询问，`deny` 始终生效。**注意：** 显式写 `--allow-write --allow-command` 而不用 `--mode full` 时**不会**默认询问。改动前开始、尚未完成的 full 模式回合续跑时会提示配置已变化：加 `--auto-approve` 照旧，或 `--accept-config-changes` 接受新规则。见 [docs/tool_annotations.md](docs/tool_annotations.md)。
- **CLI / TUI / Web 下写文件默认不审批。** 要逐次批准，用 `--tool-policy` 把对应工具设为 `ask`；策略按工具名精确匹配，要拦住全部写入必须同时列出 `write_file`、`edit_file`、`apply_patch`。`--tool-policy` 只能收紧，写 `allow` 打不开 `--mode` 没给的能力。`--backend docker` 下 `--mode` 和 `--tool-policy` 的含义不变，docker 不放宽任何权限，只是命令和文件操作在容器里执行，见 [docs/docker_backend.md](docs/docker_backend.md)。
- 审批绑定调用 ID、轮次、工具名和参数原文、策略指纹；批准不会授予工作区没开的能力。
- **例外：Chainlit 界面**（`uv run --extra ui baseagent-ui`，默认端口 8000）启动时自动生成策略，写入、编辑、`apply_patch` 和命令每次都要点「允许」。它每条消息跑一次性 CLI，没有主 Agent / Worker 和 `/continue`，也不能传 `--mode`、`--tool-policy`、`--watchdog`、`--harness-url`。

## 机器可读输出

一次性任务和 `--resume` 可用 `--output-format json`（结束时 stdout 只有一个对象：`status`、`final_answer`、`error`、`session_id`、`usage`、`exit_code`、`pending_approval`、`schema_version`）或 `jsonl`（每行一个元数据事件，最后一行 `type: "result"`）。日志、trace 和 Worker 输出都走 stderr。退出码：`0` 本轮完成，`1` 未完成或会话错误，`2` 参数错误，`130` 被中断，`3` 是 `--backend docker` 起不来（调用模型之前，不会退回 local）。`--backend docker` 时结果多一个 `backend` 对象（jsonl 另有一行 `backend_ready`），没有这个键就是 local。对话模式和其他运维操作带这个参数会报错。字段和版本规则见 [docs/output.md](docs/output.md)。

```bash
baseagent --root . --mode edit --output-format json "Rename the helper"
baseagent --session S --resume --output-format jsonl | jq -c 'select(.type == "result") | .data.status'
```

## 安全边界

- **LocalBackend（默认）不是沙箱**：开了 `--allow-command`，命令就有当前系统账户的全部文件和网络权限。只在可信的任务和工作区里开启。
- **`--backend docker`**：一次运行一个容器，默认断网，工作区挂到 `/workspace`。Docker 起不来时退出码 3，不会退回 local。容器与宿主共享内核；工作区是 bind mount，命令改动 `/undo` 撤不回；MCP 服务器仍在宿主上跑。真实冒烟尚未跑。见 [docs/docker_backend.md](docs/docker_backend.md)。

| 组件 | 管什么 | 时机 | 默认 |
| --- | --- | --- | --- |
| Watchdog（内置） | 跨步骤的停滞、重复、预算、超时 | 先给提示，超限后以 `aborted_by_watchdog` 结束 | 关闭；`--watchdog` 打开，阈值见 [docs/watchdog.md](docs/watchdog.md) |
| HarnessKnight（外部服务，可选） | 单次提示 / 工具调用的安全判定 ALLOW / REVIEW / BLOCK | 工具进入账本之前；REVIEW 转为逐次审批；提示被 BLOCK 时不发模型请求 | 关闭；`--harness-url URL` 打开，`--harness-fail-mode` 默认 `closed`（服务不可用即拦截），`--harness-timeout` 默认 3 秒；需要鉴权时设 `HARNESS_API_TOKEN` |

两者职责不同、互不依赖：Watchdog 只读取 HarnessKnight 留下的决策计数，没有 HarnessKnight 也能用。

作用范围：两者都作用于一次性任务、连续对话的主 Agent 和它派出的每个 Worker（Worker 继承入口的中间件、审批策略和 Watchdog 设置）；这条路径目前没有专门测试。

- Watchdog 按会话计数：每个 Worker 的 `--watchdog-max-*` 单独计算，不是整轮总额。
- 停滞规则只把写入（`write_file` / `edit_file` / `apply_patch`）和 `verify_command` 算作进展；只读 Worker 和主 Agent 连续 5 步没有这两类调用会被停止。只读任务请设 `--watchdog-stall-abort-steps 0`。
- HarnessKnight 对 Worker 的调用判 REVIEW 时，Worker 进入等待审批，在对话里逐条确认。

## 库接口

内核 `iter_agent(model, state, *, tools, ...)` 只依赖标准库和 `jsonschema`；`run_agent(...)` 在其上提供持久化、审批、预算和完成门槛。模型适配器有 `OpenAICompatModel` 和 `AnthropicModel`，`CassetteModel` 提供严格的录制 / 回放。

```python
from baseagent.agent import KernelState, iter_agent
from baseagent.kernel import ToolRegistry

class Model:
    def complete(self, messages, tools):
        return {"content": "done"}

state = KernelState(messages=[{"role": "user", "content": "Say done."}])
for event in iter_agent(Model(), state, tools=ToolRegistry()):
    print(event.type)
print(state.status, state.final_answer)
```

适配器、重试与限流、录制回放、子任务、事件日志、取消、完成门槛、会话记忆、工具策略规则和 `WorkspaceBackend` 协议见 [参考手册](docs/reference.md)；模块地图和路线见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 文档索引

| 主题 | 文档 |
| --- | --- |
| 完整行为契约、默认值与限制 | [docs/reference.md](docs/reference.md) |
| 主 Agent 与 Worker | [docs/router_worker.md](docs/router_worker.md) |
| TUI / Web 前端 | [docs/ui.md](docs/ui.md) |
| 检查点与撤销 | [docs/checkpoints.md](docs/checkpoints.md) |
| `apply_patch` | [docs/patch.md](docs/patch.md) |
| JSON / JSONL 输出 | [docs/output.md](docs/output.md) |
| MCP | [docs/mcp.md](docs/mcp.md) |
| 流式与并行 | [docs/async_react_streaming.md](docs/async_react_streaming.md) |
| Watchdog | [docs/watchdog.md](docs/watchdog.md) |
| Trigger | [docs/triggers.md](docs/triggers.md) |
| Docker 沙箱 / `--backend docker` | [docs/docker_backend.md](docs/docker_backend.md) |
| 评测入口 | [docs/eval.md](docs/eval.md) |
| Terminal-Bench | [docs/terminal_bench.md](docs/terminal_bench.md) |
| Project Assistant | [applications/project_assistant/README.md](applications/project_assistant/README.md) |
| 架构与路线 | [ARCHITECTURE.md](ARCHITECTURE.md) |

## 运行测试

测试不需要密钥，不联网。全量用 pytest（`uv sync` 默认装上 dev 依赖组里的 pytest）：有 9 个模块用了 pytest 的 fixture，`unittest` 加载不了它们；其余用 `unittest` 写的模块 pytest 也照常运行。

```bash
.venv/bin/python -B -m pytest tests -q                                     # 全量
.venv/bin/python -B -m unittest discover -s tests -p "test_kernel*.py" -v  # 内核
.venv/bin/python -B -m unittest discover -s tests -p "test_rag*.py" -v     # RAG
```

`unittest discover` 仍能跑用 `unittest` 写的模块，但会把那 9 个 pytest 模块报成导入失败，不能当全量。两种跑法都不会写真实的 MCP 信任库（`tests/config_isolation.py`）。
