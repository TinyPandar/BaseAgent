# Project Assistant Phase 3 开发计划

基线：`master` HEAD `e4b4437`（2026-10-19 11:38 UTC+8，Phase 2 `knowledge_search` 工具包）。本计划只依据该提交中的代码与文档；开工前需重新核对 HEAD、工作区与全量测试。

Phase 1/2 已交付：切分（`rag/chunking.py`）、Embedding 接口（`rag/embedding.py`：`OpenAIEmbedding` + `MockEmbedding`）、Chroma 索引（`rag/indexing.py`）、证据构造（`rag/retrieval.py`）、语料收集与版本（`corpus.py`）、来源权限（`access.py`）、`knowledge_search` 工具包（`tools.py`），以及离线演示 `scripts/demo_knowledge_search.py`。应用 README 明确列为待办的是：真实模型回答质量、Recall@k、受控 coding 场景的端到端评测。

## 优先级总览

| 优先级 | 项目 | 理由 |
| --- | --- | --- |
| P0 | 1. 真实 embedding 检索评测 | 目前只有 Mock embedding，检索质量没有任何证据；后续各项都依赖“检索可用” |
| P1 | 2. Project Assistant CLI 入口 | 应用目前只能用 Python 手工装配；验收场景 2、3 需要可复现入口 |
| P1 | 3. 受控 coding 验收场景 | 应用 README 验收场景 3，项目展示的核心闭环 |
| P2 | 4. git 历史进度恢复 | 验收场景 2；需要新增语料来源，范围最大，放最后 |

---

## 1. 真实 embedding 检索评测（P0）

**目标**：用真实 embedding 在固定语料版本上测出可复现的检索指标（Recall@k、MRR），并与 Mock 基线对比，作为后续调优的起点。

**涉及模块**
- `applications/project_assistant/rag/embedding.py`（`OpenAIEmbedding`：目前固定 `self._dimension = 1536`，`OpenAI(api_key=...)` 未支持 `base_url`）
- `applications/project_assistant/config.py`（`RAGConfig.from_env`）
- `applications/project_assistant/rag/indexing.py`、`rag/retrieval.py`
- `scripts/build_knowledge_base.py`、`scripts/query_knowledge_base.py`
- 新增建议：`applications/project_assistant/evaluation/`（问题集、评测脚本、报告）
- 参考：`src/baseagent/evaluation.py`（现有离线评估的报告格式）

**任务拆解**
1. 建问题集：30～50 题（DEVELOPMENT_HANDOFF 的建议），覆盖精确标识符、概念解释、跨文件证据、无答案、过期资料；每题标注期望证据 `file_path:line_start-line_end`；按调优集 / 测试集分开。
2. 评测脚本：读取问题集 → 对指定索引检索 → 计算 Recall@k（k=1/3/5）、MRR；输出带 `corpus_version`、embedding 模型、`top_k` 的 JSON 报告（不覆盖已有报告）。
3. Embedding 适配：维度由模型返回值推断或改为可配置；视需要为 `OpenAIEmbedding` 增加 `base_url`（用于兼容端点），并以环境变量暴露。
4. 先跑 Mock 基线，再跑真实 embedding（付费调用需用户单独授权）；记录调用次数与成本。
5. 视结果做候选实验（应用 README 后续方向）：BM25 混合检索、重排、chunk 预算。

**验收标准**
- 评测脚本在 Mock 下离线可运行，并有单元测试（指标计算、无答案题处理）。
- 产出一份真实 embedding 报告：固定语料版本、测试集 Recall@5 与 MRR、实际 API 调用次数。
- 文档写明实际得到的数值（不预设目标值），区分调优集与测试集结果。

**风险**
- 付费 API 与凭据：需显式授权；`OPENAI_API_KEY` 不能进入索引、报告或录像。
- 维度硬编码 1536：换模型（如 `text-embedding-3-large`）会与 Chroma 集合维度冲突。
- 标注成本高、问题集偏差；语料改动会让标注行号失效（需绑定 `corpus_version`）。
- 入库/查询脚本不加载 `.env`，容易误以为配置生效。

---

## 2. Project Assistant CLI 入口（P1）

**目标**：一条命令完成“入库 → 带 `knowledge_search` 的 agent 问答”，复用现有 `run_agent`、`SessionStore` 与 CLI 参数语义，不另写执行循环。

**涉及模块**
- 新增建议：`applications/project_assistant/app.py`（DEVELOPMENT_HANDOFF 原建议的装配入口）
- `applications/project_assistant/tools.py`（`knowledge_package`）、`access.py`（`SourceAccess`）
- `src/baseagent/capabilities/assembly.py`（`assemble_capabilities(..., extensions=[...])`）
- `src/baseagent/main.py`（模型初始化逻辑：`DEEPSEEK_*`；`--tool-package` 目前只接受 `coding/memory/shared-memory`）
- `pyproject.toml`（`[project.scripts]`；目前 `applications/` 不在发行包内，uv_build 只打包 `src/baseagent`）

**任务拆解**
1. 决定入口形态：A) 仓库内脚本 / `python -m applications.project_assistant`（不改打包）；B) 新增 console script（需调整打包范围）。建议先 A。
2. 子命令：`index`（入库，参数与 `build_knowledge_base.py` 一致）、`ask`（问答）、`status`（显示索引的 `corpus_version` 与语料统计）。
3. 装配：`Workspace(root)` → `SourceAccess` → `knowledge_package(index, access, corpus_version=...)` → `assemble_capabilities(packages=[...], extensions=[...])` → `run_agent`；模型沿用 `main.py` 的 DEEPSEEK 配置方式。
4. 索引与语料版本不一致时拒绝运行，提示重新入库（遵守 Phase 2“禁止静默替换索引”约定）。
5. 用 `ScriptedModel` 风格的假模型和 Mock embedding 写端到端测试。

**验收标准**
- 无密钥时：`index --mock-embedding` 与使用假模型的 `ask` 测试离线通过。
- 有授权时：一次真实问答输出带可定位引用（`path:start-end`）的答案，会话可通过 `--inspect-session` 查看。
- 默认 CLI（`baseagent`）的行为和全量测试不受影响。

**风险**
- 打包边界：把 `applications/` 放进发行包会改变 wheel 内容与 README 中的“仅从 checkout 运行”的说法。
- 两套权限（`SourceAccess` 与 `ToolPolicy`）需配置一致，否则可能出现“可检索但不可读”或反过来的情况。
- 与 `main.py` 重复参数解析，后续容易漂移；尽量复用 `load_harness_config`。

---

## 3. 受控 coding 验收场景（P1）

**目标**：完成应用 README 验收场景 3：根据检索资料修改代码/文档、运行实际验证、汇报结果；全程受权限、预算和完成策略约束，可离线回放。

**涉及模块**
- `src/baseagent/tools/`（编辑、命令、`workflow.py` 计划/验证）、`tools/policy.py`
- `src/baseagent/agent/completion.py` 与 CLI `--completion-policy`（require_plan、精确命令检查、产物）
- `src/baseagent/evaluation.py`（现有 `coding` 离线场景可作模板）
- `src/baseagent/testing/cassette.py`（`CassetteModel` 录制/回放）
- `applications/project_assistant/tools.py`、上面第 2 项的入口
- `examples/harness.coding.json`、`examples/skills/coding-check/`

**任务拆解**
1. 设计固定的合成任务：在临时工作区复制小型语料与一个故意失败的测试，要求 agent 先 `knowledge_search` 找到相关约定再修复。
2. 编写 completion policy：必须有计划、必须运行指定测试命令且通过、必须生成指定产物。
3. 先用脚本模型实现离线端到端测试；再在授权后用真实 DeepSeek 录制合成录像（参照 `scripts/record_cassettes.py` 的调用次数上限做法），断网回放。
4. 记录指标：是否通过外部验收、模型/工具尝试数、tokens、是否出现未授权或重复副作用。

**验收标准**
- 离线测试：检索调用出现在工具账本中，补丁使目标测试通过，completion policy 判定完成；写/命令权限只在该场景显式开启。
- 真实录像（如获授权）：脱敏扫描通过，断网回放状态一致。
- 失败路径：权限拒绝或验证失败时状态不是 completed。

**风险**
- `LocalBackend` 不是 OS 沙箱；真实运行必须在临时目录，不能在用户仓库。
- 回放只重放模型响应，工具仍会实际执行，副作用需隔离。
- 真实模型结果不稳定，单次成功不能说明可靠性；需多次运行并如实报告。

---

## 4. git 历史进度恢复（P2）

**目标**：完成应用 README 验收场景 2：整合文档与 git 历史，回答“当前基线状态与待实施项”，并区分历史陈述（文档/提交信息）与当前证据（代码）。

**涉及模块**
- `applications/project_assistant/corpus.py`（目前只收集文件；默认排除 `.git`）
- `applications/project_assistant/rag/chunking.py`（需新增提交类 chunk 类型）
- `applications/project_assistant/access.py`（来源权限需覆盖新来源）
- `src/baseagent/tools/`（现有 coding 工具已能查看 `git diff`）
- 新增建议：`applications/project_assistant/history.py`

**任务拆解**
1. 方案选择：A) 入库时用 `git log` 生成只读的提交摘要 chunk（hash、日期、作者、标题、改动文件列表），以提交 hash 作为引用；B) 新增只读的 `project_history` 工具实时查询。建议先 A，并只读取提交元数据和统计，不索引完整 diff。
2. 证据结构：提交类证据带 `commit:<hash>` 引用，与文件证据 `path:start-end` 区分；`corpus_version` 包含 HEAD hash。
3. 提示与回答：要求回答标注“历史记录”和“当前代码”两类来源；文档声明与代码冲突时以代码为准并指出冲突（例如 DEVELOPMENT_HANDOFF 旧版中“目录不存在”的说法）。
4. 评测：在第 1 项问题集中加入“进度类”问题，例如“Phase 2 完成了什么”“哪些仍待实现”。

**验收标准**
- 在合成 git 仓库（测试内 `git init`）上离线测试：提交 chunk 可被检索、引用可定位、HEAD 变化后语料版本改变。
- 对本仓库回答“当前进度”时，至少同时引用一条提交和一个当前文件证据。
- 不读取 `.git` 内部文件，不引入新的第三方依赖（用 `git` 命令或标准库）。

**风险**
- 提交信息里可能出现凭据或私人信息，需复用 `Redactor` 脱敏。
- 调用 `git` 子进程：要处理 git 未安装、非 git 目录、Windows 路径等情况。
- 提交信息与文档都可能过时，需要明确告诉模型“历史陈述 ≠ 当前事实”。
- 范围容易扩大成项目看板（ARCHITECTURE 中的个人 assistant 规划），本阶段不做。

---

## 共同约定（来自 ARCHITECTURE.md / DEVELOPMENT_HANDOFF.md）

- 不修改 kernel；RAG 与业务状态留在 `applications/`。
- 新增第三方依赖、重大边界变化、付费调用需先说明并取得授权。
- 每项交付都要跑专项测试与全量回归，并区分假模型、离线回放、真实 API 三类证据。
