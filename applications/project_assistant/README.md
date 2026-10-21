# Project Assistant - RAG-based Knowledge & Coding Agent

个人开发者的知识与项目助理,支持项目资料检索、带引用回答、一次受控 coding 任务。

## CLI 入口（Phase 3 / P1）

仅从仓库 checkout 运行，不改变发行包或默认 `baseagent` CLI。入口复用
`baseagent.main` 的配置、DEEPSEEK 模型初始化、`SessionStore` 和 `run_agent`。

```bash
# 离线入库；参数及相对路径语义与 scripts/build_knowledge_base.py 一致
.venv/bin/python -m applications.project_assistant index --corpus-root . \
  --output-dir .baseagent/rag_index --metadata-file .baseagent/corpus_metadata.json \
  --mock-embedding

# 无模型、无 embedding API 的索引状态检查
.venv/bin/python -m applications.project_assistant status --corpus-root . \
  --index-dir .baseagent/rag_index

# 问答示例：需要另行授权的真实 DEEPSEEK 调用；此示例未作为离线验证执行
.venv/bin/python -m applications.project_assistant ask "解释 knowledge_search 的来源校验" \
  --root . --index-dir .baseagent/rag_index --mock-embedding --session project-demo

# 追加同一会话的新问题 / 检查持久化状态及工具账本
.venv/bin/python -m applications.project_assistant ask "引用对应代码行" \
  --root . --mock-embedding --session project-demo
.venv/bin/python -m applications.project_assistant ask --session project-demo --inspect-session
.venv/bin/python -m applications.project_assistant ask --help
```

`index` 支持 `--corpus-root`、`--output-dir`、`--metadata-file`、`--mock-embedding`，
显式重建索引并在成功后把 `corpus_version` 保存到 Chroma 集合。
`status` 输出索引版本、索引 chunk 数、当前语料版本/文件数/chunk 数及
`matches_current_corpus`；索引不存在时仍可检查，返回空版本和 false，不创建索引。
`ask` 的 `--index-dir`（及 `status`）默认使用 `RAG_INDEX_DIR` 或
`.baseagent/rag_index`；路径相对当前进程目录，语料根目录由 `--root`、配置或已有会话决定。
索引缺失、空索引、未绑定版本的旧索引或版本不一致时，`ask` 非零退出并提示运行
`index`，不会自动重建。语料修改后必须重新入库。

问答默认 hybrid，沿用 `RAG_RETRIEVAL_MODE` 等现有配置。`--mock-embedding`
仅替换 embedding，不替换问答模型；生产问答沿用 `.env` 中的
`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`，涉及真实调用时需另行授权。
`ask --config`、`--db`、`--session`、`--resume`、`--inspect-session`、预算、
能力包及权限开关与原 CLI 一致；检查会话不需要索引、模型或密钥。
默认不开启写入或命令权限。

来源可见性使用 `SourceAccess` 的 Markdown/Python 包含规则及入库排除规则，
并额外服从同一 `ToolPolicy` 的 `read_file` 路径规则。deny 或 ask 的来源不会作为
检索证据返回；一次 read_file 批准不授权批量检索。无法表示为路径规则的
read_file 策略（例如行号条件）显式拒绝装配。`knowledge_search` 自身也受工具策略控制。

离线端到端测试使用 `main(argv, model_factory=lambda args: ScriptedModel(...))`
注入假模型，在临时目录执行 index → status → ask，并验证引用、会话、权限及版本拒绝。
这个工厂仅影响显式注入的调用；生产入口不注入模型。

```bash
.venv/bin/python -m pytest -q tests/test_project_assistant_cli.py
```

## 架构概览

```
applications/project_assistant/
├── rag/                       # RAG 核心组件
│   ├── chunking.py           # 文档与代码切分
│   ├── embedding.py          # Embedding 接口 (OpenAI + Mock)
│   ├── indexing.py           # 向量索引 (ChromaDB)
│   └── retrieval.py          # 检索与证据构造
├── config.py                  # RAG 配置
├── corpus.py                  # 语料收集与版本管理
├── access.py                  # 宿主配置的来源可见范围
└── tools.py                   # knowledge_search / ToolPackage
```

## 验收场景 (目标)

1. **理解项目机制** - 检索 BaseAgent 文档与代码,解释技术实现,展示可定位来源
2. **项目进度恢复** - 整合文档与 git 历史,回答当前基线状态与待实施项
3. **执行开发任务** - 根据检索资料,修改代码/文档,运行验证,汇报结果

## Phase 1 完成情况 ✓

### 已实现

- [x] 文档切分 (Markdown 按标题层级,Python 按函数/类)
- [x] Embedding 接口 (OpenAI + 确定性 Mock)
- [x] 向量索引 (ChromaDB 持久化)
- [x] 检索与证据构造 (Evidence 数据类,包含引用信息)
- [x] 语料收集 (排除 .git、凭据,版本哈希)
- [x] 配置管理 (环境变量 + 默认值)
- [x] 入库脚本 (`scripts/build_knowledge_base.py`)
- [x] 查询脚本 (`scripts/query_knowledge_base.py`)
- [x] 单元测试 (15 个测试,全部通过)

### 测试覆盖

```bash
# 运行所有 RAG 测试
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_rag*.py" -v

# 测试入库 (使用 mock embedding,无 API 调用)
.venv\Scripts\python.exe -B scripts/build_knowledge_base.py --mock-embedding

# 测试查询
.venv\Scripts\python.exe -B scripts/query_knowledge_base.py "kernel architecture" --mock-embedding
```

### Phase 1 历史语料统计

- **文件数**: 142
- **Chunk 数**: 1840
- **Chunk 类型**: markdown_section, python_function, python_class, python_module
- **元数据**: 文件路径、行号范围、父章节、函数/类名

### 依赖

```toml
# 实际约束以仓库 pyproject.toml / uv.lock 为准
chromadb>=0.6.3
openai>=3.15.0
```

Phase 2 没有新增第三方依赖；当前没有使用 tiktoken。

## Phase 2 完成情况（2026-10-19）

- [x] `knowledge_package` 通过现有 `ToolPackage` 注册 `knowledge_search(query, top_k=5)`。
- [x] `SourceAccess` 在返回前检查宿主允许/排除的文件范围及 backend 保护路径；拒绝隐藏目录、凭据文件、路径穿越、链接与 Windows junction 别名。
- [x] 对当前源文件的引用行、片段内容摘要及文档 ID 进行核对；已删除、不可读或片段已变化的证据不返回。
- [x] 返回前脱敏正文与章节等元数据，敏感内容不会先以原文进入工具消息、SQLite 检查点或模型录像。
- [x] 确定性模型的录制/离线回放覆盖真实 Chroma 查询及工具执行，保留严格请求匹配。

返回结构包含 `evidence`、`corpus_version`、实际 `retrieval_mode`、`score_kind` 分数语义及 `reference_only`。每条证据包含可定位引用、片段摘要和当前文件 SHA256，并标记脱敏/截断。检索文本是工具参考数据，不提升为系统指令；模型仍可能受提示词注入影响，本阶段没有宣称解决语义层注入问题。

宿主通过 `SourceAccess(workspace, include_patterns, exclude_patterns)` 配置知识权限，默认允许当前工作区内的 `*.md`、`*.py`，同时强制排除私有路径。`include_patterns=()` 表示全部不可见。该 ACL 独立于 coding 的 `ToolPolicy`：宿主需为两者配置一致的文件范围；`ToolPolicy` 仍可对整个 `knowledge_search` 设置 allow/ask/deny，注册工具不会扩大写文件或命令权限。

入库复用相同排除规则，修复多段路径和 `*.egg-info` 通配符匹配，并实际遵守 `include_patterns`。语料版本现在包含路径及行号，文件改名也会改变版本；Phase 1 的旧版本号需重新生成。索引数据本身由宿主管理，禁止在运行中的工具包背后静默替换索引；重新入库后用新语料版本重新构造工具包。来源范围、backend、索引配置、语料版本、脱敏规则变化会改变包版本，未完成会话恢复时复用 harness 的配置漂移检查。

脱敏复用 `Redactor`，补充凭据赋值、私钥、常见 GitHub/AWS token 模式，以及敏感环境变量值；可通过 `secrets=`、`redaction_patterns=` 补充。规则属于尽力过滤，不能保证识别所有未知格式的秘密；不要将私有资料放入授权语料。索引磁盘内容不因工具出口脱敏而自动清洗。来源文件读取限额沿用 backend 的 200 KB；每个结果正文至多 6000 字符，证据列表至多约 48 KB。工具最多检索 `top_k * 4` 个候选后做可见性过滤，因此有效结果可以少于 k；空结果不等于整个语料中绝无答案。

### 最小工具装配（离线）

```python
from pathlib import Path
from applications.project_assistant.access import SourceAccess
from applications.project_assistant.corpus import collect_corpus, compute_corpus_version
from applications.project_assistant.rag.embedding import MockEmbedding
from applications.project_assistant.rag.indexing import VectorIndex
from applications.project_assistant.tools import knowledge_package
from baseagent.capabilities import assemble_capabilities
from baseagent.tools.workspace import Workspace

root = Path(".").resolve()
chunks = collect_corpus(root, include_patterns=["README.md"])
index = VectorIndex(MockEmbedding(32), collection_name="knowledge_example")
index.clear()
index.add_chunks(chunks)
workspace = Workspace(root)
tools = assemble_capabilities(workspace, packages=["coding"], extensions=[
    knowledge_package(index, SourceAccess(workspace, ("README.md",)),
                      corpus_version=compute_corpus_version(chunks)),
])
result = tools.execute("knowledge_search", {"query": "kernel architecture", "top_k": 3})
print(result.to_dict())
# 已有模型对象可直接用 run_agent(model, prompt, tools=tools)，不另写执行循环。
```

以上应用模块从仓库 checkout 导入，尚未作为独立发行包或默认 CLI 模组发布。Mock embedding 仅验证机制，不证明语义相关性。完整离线演示使用临时合成语料，无付费调用：

```powershell
.venv/Scripts/python.exe -B scripts/demo_knowledge_search.py
.venv/Scripts/python.exe -B -m unittest discover -s tests -p "test_rag*.py" -v
```

Phase 2 新增 20 项测试，与 Phase 1 合计 35 项 RAG 测试通过；最终全量回归 497 项全部通过（235.373 秒）。演示的 7 项检查通过，包括回放时再次执行检索、状态一致、引用定位和写权限保持关闭。这里的模型响应和 usage 为合成值；真实模型回答质量、Recall@k 和受控 coding 场景的端到端评测仍待后续阶段。

## 使用示例

### 构建知识库 (本地,无 API 调用)

```bash
.venv\Scripts\python.exe -B scripts/build_knowledge_base.py \
  --mock-embedding \
  --output-dir .baseagent/rag_index \
  --metadata-file .baseagent/corpus_metadata.json
```

### 构建知识库 (OpenAI Embedding)

```bash
# 设置 API key
$env:OPENAI_API_KEY = "sk-..."

.venv\Scripts\python.exe -B scripts/build_knowledge_base.py \
  --output-dir .baseagent/rag_index \
  --metadata-file .baseagent/corpus_metadata.json
```

### 查询知识库

```bash
.venv\Scripts\python.exe -B scripts/query_knowledge_base.py \
  "how does tool retry work" \
  --mock-embedding \
  --top-k 5
```

查询脚本也通过 `knowledge_search` 的可见性及脱敏边界；可用 `--corpus-root` 指定对应源目录，重复 `--include "src/*.py"` / `--exclude "private/*"` 缩小范围。交互查询没有持久会话，其语料版本显示为未跟踪的交互查询标识；持久 Agent 应使用入库时实际的 `compute_corpus_version` 值。旧索引若缺少片段摘要或引用已失效，应重新入库。

## 配置

通过环境变量配置:

```bash
# OpenAI-compatible Embedding（阿里云百炼示例）
RAG_EMBEDDING_PROVIDER=openai
RAG_EMBEDDING_API_KEY=sk-...
RAG_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RAG_EMBEDDING_MODEL=text-embedding-v4
RAG_EMBEDDING_BATCH_SIZE=10
# 可选：请求指定维度
# RAG_EMBEDDING_DIMENSIONS=1024

# 检索配置（默认值；也支持 vector / bm25）
RAG_RETRIEVAL_MODE=hybrid
RAG_RRF_K=30
RAG_VECTOR_WEIGHT=1
RAG_BM25_WEIGHT=2
RAG_CANDIDATE_DEPTH=20
RAG_BM25_K1=1.5
RAG_BM25_B=0.75

# 索引配置
RAG_COLLECTION_NAME=baseagent_docs
RAG_INDEX_DIR=.baseagent/rag_index
```

`RAG_EMBEDDING_API_KEY` 优先于 `OPENAI_API_KEY`；批次大小默认为 100，百炼请设为 10（每次请求最多 10 个输入）。`RAG_EMBEDDING_DIMENSIONS` 可省略，兼容旧名 `RAG_EMBEDDING_DIMENSION`，两者同时设置时优先使用新名。构建、查询和评估脚本读取同一组配置，不自动加载 `.env`；更换模型或维度后须重建索引。

脚本通过 `--corpus-root`、`--index-dir`、`--top-k` 等参数选择路径与数量；这些参数不自动读取同名的其他环境变量。

`RAGConfig()`、`RAGConfig.from_env()`、`retrieve`、`knowledge_search` 和查询脚本默认使用 hybrid。非法 `RAG_RETRIEVAL_MODE` 会抛出包含配置项、输入值和允许值的 `ValueError`。工具包创建时绑定检索配置；更改配置后应重新构造工具包。

hybrid 使用 BM25 与向量候选的加权 RRF。BM25 默认从向量 collection 的正文和元数据构建，也可向 `retrieve` 传入可复用的 `lexical_index`。回退规则：

- BM25 语料为空、读取/构建/查询失败：回退 vector，返回原始距离（越低越好），`min_score` 是距离上限。
- 向量索引缺失（`retrieve(..., index=None, lexical_index=...)`）、为空、计数/查询失败或无候选，且 BM25 可用：回退 bm25，返回原始 BM25 相关性（越高越好），`min_score` 是相关性下限。
- 每个进程对每种回退方向仅记录一次 warning，不输出语料或底层异常中的敏感信息。空索引没有可检索内容时返回空证据；BM25 不可用且向量操作也失败则报错。显式 vector/bm25 不切换模式，操作失败直接报错。

正常 hybrid 的 RRF 分数越高越好，`min_score` 为相关性下限。`knowledge_search` 返回实际模式和对应的 `score_kind`，查询脚本按实际分数显示 distance/relevance。回退后应按实际分数语义解释结果，不混用阈值。

默认值依据[80 题集评测报告](evaluation/reports/bailian-80q-summary-2026-10-21.md)：test 上 vector R@1/MRR 为 **0.378/0.678**，bm25 **0.407/0.759**，hybrid **0.567/0.845**，hybrid+qwen **0.583/0.857**。参数在 tune 上选择，test 使用固定参数。qwen 的收益小且每查询约 **1.4 万 token**，故保持可选、默认关闭；只有显式向 `retrieve` 传入 reranker 才会重排，评测 API 还需 `--allow-rerank-api --max-rerank-tokens N` 显式启用。普通 hybrid 不调用重排 API；向量分支沿用配置的 embedding provider，离线测试使用 Mock。本次没有重跑付费评测。

## 技术选型

| 组件 | 技术 | 理由 |
|------|------|------|
| Embedding | OpenAI text-embedding-3-small | 1536维,成本低,质量好 |
| 向量索引 | ChromaDB | 简单,持久化,Python 原生 |
| 切分策略 | Markdown 按标题,Python 按函数 | 保留语义边界,支持精确引用 |
| Mock Embedding | 确定性哈希 + 随机投影 | 测试时无 API 依赖,可复现 |

## 设计约束

1. **引用精确性**: 每个 Evidence 必须包含 `file_path:line_start-line_end`
2. **版本管理**: 语料版本由所有文件内容哈希计算,增量更新依赖此值
3. **权限遵守**: 检索结果不得包含 `.env`、凭据文件、git 内部
4. **测试隔离**: 所有测试使用 mock embedding,不调用外部 API

## 后续优化方向

- [x] 默认混合检索 (BM25 + 向量 RRF，可选重排)
- [ ] Chunk 预算控制 (限制 top-k tokens ≤ N)
- [ ] 增量索引更新 (基于 corpus version diff)
- [ ] Cross-encoder 重排 (可选)
- [ ] 评测框架 (Recall@5, 引用准确率)
