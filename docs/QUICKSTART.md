# QUICKSTART · 5 分钟上手

## 0. 环境要求

- Python ≥ 3.10（推荐 3.11/3.12）
- Windows / macOS / Linux 均可（本项目在 Windows 上开发验证）

## 1. 安装

```bash
git clone https://github.com/LZNJUPT/if_we_elsewhere.git
cd if_we_elsewhere
pip install -r requirements.txt
```

可选增强（不装自动降级）：

```bash
pip install fastembed      # 本地向量检索（混合记忆召回）
```

## 2. 先用示例数据体验（不需要任何真实数据、不需要 API Key）

```bash
python run.py demo
```

会发生什么：

1. 用 `sample_data/` 里的**虚构**对话（79 条，与任何真实人物无关）在 `data_demo/`
   建一套独立演示库（与真实数据完全隔离）；
2. 离线跑完分析（启发式事件 + 关系状态估计 + 转折点）；
3. 装载示例人格档案，并启动界面 `http://127.0.0.1:8015`。

界面怎么玩：

- 左侧「＋ 新的一条线」→「接着往下聊」：在示例记录结束的次日继续与数字人格对话
  （发消息需要 API Key，见下）；
- 「回到过去某天」：在时间轴上点一个月份 → 写一句「这次你想怎么做」→ 开一条 IF 线；
- 「推进一天」：时间前进，查看关系五维状态的变化。

## 3. 配置 LLM（解锁对话生成）

对话推演由 LLM 驱动。默认 DeepSeek，任意 OpenAI 兼容端点均可：

```bash
# 方式一：环境变量（推荐；config llm.api_key_env 可改名）
export LLM_API_KEY=sk-...            # Windows PowerShell: $env:LLM_API_KEY="sk-..."

# 方式二：改 config.yaml 的 llm 段（base_url/model/provider），密钥仍走环境变量
```

> 没有 API Key 也能浏览界面、看时间轴与真实记录回放；发消息时会得到明确提示。

## 4. 导入你自己的数据

```bash
python run.py init          # 生成 config.yaml（首次运行）
python run.py init --source 你的记录.jsonl \
    --sender-a "导出文件里你的账号名" \
    --sender-b "导出文件里对方的账号名"
python run.py analyze       # 生成事件/记忆/关系状态/转折点/人格（需要 API Key）
# 无 API Key：python run.py analyze --skip-llm（启发式事件 + 手写 persona 模板）
```

格式说明见 [IMPORT.md](IMPORT.md)。

## 5. 日常启动

```bash
python run.py server        # http://127.0.0.1:8015（仅本机可访问）
```

进阶 CLI（可选）：

```bash
python app/phase15_dial_engine.py new --start 2024-06-20 --divergence 2024-06-26 \
    --rewrite "那天我直接打了电话过去" --name "如果那天我打了电话"
python app/phase15_dial_engine.py repl --line last
python app/phase15_dial_engine.py selftest      # 离线全链路自检（不需要 API Key）
python app/phase4_retrieval.py --demo           # 记忆检索演示
```

## 常见问题

**Q: `python run.py demo` 会碰我的真实数据吗？**
不会。demo 全部产物写入 `data_demo/`，真实数据目录是 `data/`，二者互不可见。

**Q: 端口被占用？**
`python run.py server --port 8088`，或改 `config.yaml` 的 `defaults.port`。

**Q: 数据库在哪？怎么备份？**
`data/ifwe_v1.db`（demo 是 `data_demo/ifwe_v1.db`）。备份 = 复制这个文件。

**Q: 想推倒重来？**
删除 `data/` 后重新 `init`（源 JSONL 不受影响，随时可重导入）。
