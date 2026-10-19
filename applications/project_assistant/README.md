# Project Assistant - RAG-based Knowledge & Coding Agent

个人开发者的知识与项目助理,支持项目资料检索、带引用回答、一次受控 coding 任务。

## 架构概览

```
applications/project_assistant/
├── rag/                       # RAG 核心组件
│   ├── chunking.py           # 文档与代码切分
│   ├── embedding.py          # Embedding 接口 (OpenAI + Mock)
│   ├── indexing.py           # 向量索引 (ChromaDB)
│   └── retrieval.py          # 检索与证据构造
├── config.py                  # RAG 配置
└── corpus.py                  # 语料收集与版本管理
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

### 语料统计

- **文件数**: 142
- **Chunk 数**: 1840
- **Chunk 类型**: markdown_section, python_function, python_class, python_module
- **元数据**: 文件路径、行号范围、父章节、函数/类名

### 新增依赖

```toml
# 已添加到 pyproject.toml
chromadb = "^1.5.9"      # 向量数据库
tiktoken = "^0.9.0"      # Token 计数 (用于后续预算控制)
openai = "^1.59.8"       # Embedding API
```

## Phase 2 下一步

- [ ] 实现 `knowledge_search` 工具 (注册到 ToolPackage)
- [ ] 权限检查 (只返回用户可见文件)
- [ ] 脱敏验证 (工具结果不包含凭据)
- [ ] 工具调用测试 (录制/回放)

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

## 配置

通过环境变量配置:

```bash
# OpenAI Embedding
OPENAI_API_KEY=sk-...
RAG_EMBEDDING_MODEL=text-embedding-3-small

# 索引配置
RAG_COLLECTION_NAME=baseagent_docs
RAG_INDEX_PERSIST_DIR=.baseagent/rag_index
RAG_TOP_K=5

# 语料配置
RAG_CORPUS_ROOT=.
```

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

- [ ] 混合检索 (BM25 + 向量,重排)
- [ ] Chunk 预算控制 (限制 top-k tokens ≤ N)
- [ ] 增量索引更新 (基于 corpus version diff)
- [ ] Cross-encoder 重排 (可选)
- [ ] 评测框架 (Recall@5, 引用准确率)
