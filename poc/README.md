# poc/ —— 可选的概念验证脚本

这些脚本不参与主流程，仅作技术参考（对应 docs/ARCHITECTURE.md 中的选型结论）。
依赖较重（graphiti_core / kuzu / fastembed），**不包含在 requirements.txt**，
需要时自行安装。

| 脚本 | 内容 | 结论 |
|---|---|---|
| `graphiti_poc.py` | 用 Graphiti（Kuzu 嵌入式图库 + 本地 embedding）对聊天会话做时间化知识图谱抽取与检索 | 可行，但内存/构建开销大、中文抽取质量一般 → 主线选择自研轻量记忆（SQLite facts + 混合检索） |
| `semantic_search_demo.py` | 对已落盘的 Kuzu 图做纯本地语义检索（不调 LLM） | 同上，作为对照 |
| `inspect_data.py` | 导出文件的数据体检（只读，不输出消息正文） | 用于导入前快速确认文件格式与规模 |

## 运行前提

```bash
pip install graphiti-core kuzu fastembed python-dotenv   # 较重，按需
export LLM_API_KEY=sk-...                                # graphiti_poc 需要
```

数据源默认读 `data/raw/chat.jsonl`（同主流程），可用 `IFWE_CHAT_SOURCE` 覆盖。
图库等产物写入 `poc/data/`（已 gitignore）。
