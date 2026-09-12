# IfWe · 若我们在

<div align="center">

**把「再也没能说出口的话」，变成一次可以落地的对话推演。**

从你自己的聊天记录出发，回到任何一个想改变的时刻，改写一句话——
由基于你们真实记忆与人格的数字分身，替时间回答「如果当时……」。

[English](README_EN.md) · 快速开始 · [导入指南](docs/IMPORT.md) · [架构](docs/ARCHITECTURE.md) · [隐私设计](PRIVACY.md) · [免责声明](DISCLAIMER.md)

</div>

---

<!-- TODO(发布前): 在此处放 30 秒演示 GIF（必须用示例数据录制，严禁使用真实聊天记录！）
     录制脚本与分镜见 docs/demo/README.md -->
![demo](docs/demo/demo.gif)

## 这是什么

IfWe 是一个**本地优先**的关系时间线工具：

1. **导入**你与某个重要的人的聊天记录（你自行导出的 JSONL 文件）；
2. 系统在本地完成脱敏、事件抽取、记忆沉淀与关系状态估计；
3. 在时间轴上选一个「岔路口」——那一天你说过/没说过的一句话——**改写它**；
4. 与一个基于真实记忆与人格构建的数字人格**继续对话**，看关系可能走向哪里。

> 它不是聊天记录管理器，也不是「AI 女友」。它关心的是一个具体的问题：
> **如果当时换一种说法，会怎么样？**

## 核心特性

| 特性 | 说明 |
|---|---|
| 🔒 本地优先 | 对话、分析、数据库全部在本地 SQLite；只有「生成回复」会把脱敏上下文发给你自己配置的 LLM API |
| 🧹 导入即脱敏 | 手机号/地址/证件号/银行卡等在入库时自动替换为占位符，脱敏文本是后续分析的唯一输入 |
| 🕰 时间轴选点 | 逐月消息量、关系五维状态、转折点（断联/事件/活跃高峰）一目了然 |
| 🔀 IF 线改写 | 从任意一天切入 + 一句话改写，数字人格会「把改写当作已发生的事实」展开互动 |
| 🧠 双层记忆 | 长期记忆（双时态 facts + 可选本地向量检索）+ 短期对话缓冲（自动摘要折叠），且分歧点后自动「冻结」被改写掉的真实未来 |
| 📊 关系五维 | 亲密/冲突/信任/情绪安全/沟通质量，事件驱动增量模型，每条变化带解释链 |
| 🛡 隐私扫描门禁 | 内置 `check_privacy.py`：扫描代码与文档中的隐私残留，非零命中即拒绝发布（可挂 CI） |
| 🖼 真实表情包 | 对方真实用过的表情包按频率加权复现（配置本地表情包目录即可） |

## 3 步开始

```bash
# 0) 安装（Python >= 3.10）
pip install -r requirements.txt

# 1) 不导入任何真实数据，先用虚构示例体验完整界面
python run.py demo

# 2) 准备好你自己的数据后：生成配置 → 导入 → 分析
python run.py init
python run.py init --source 你的记录.jsonl --sender-a "你的账号名" --sender-b "对方的账号名"
python run.py analyze            # 无 API Key 可用 python run.py analyze --skip-llm

# 3) 启动本地界面（仅监听 127.0.0.1）
python run.py server             # → http://127.0.0.1:8015
```

详细步骤、数据格式与常见问题见 [docs/QUICKSTART.md](docs/QUICKSTART.md) 与 [docs/IMPORT.md](docs/IMPORT.md)。

## 隐私设计（差异化核心）

- **数据不出本机**：`data/` 永远被 gitignore；私有仓库式的内容（聊天记录、persona）与代码彻底分离。
- **脱敏边界**：发给 LLM 的只有 `content_clean`（去噪+脱敏后的文本）与派生结论，从不发送原始记录。
- **模拟隔离**：所有对话推演写入 `sim_*` 命名空间，主库只读——推演永不污染你的真实历史。
- **自查工具**：`python scripts/check_privacy.py` 用红线词表扫描整个仓库，命中即非零退出。
- 详见 [PRIVACY.md](PRIVACY.md)。

## 架构一览

```
chat JSONL ──► Phase1 导入/脱敏 ──► analyze(事件/记忆/状态/转折点/persona)
                                        │
                                        ▼
              本地 Web UI ◄──► Phase15 对话服务 ◄──► DialEngine 对话内核
              (127.0.0.1)          (FastAPI)             │
                                                        ▼
                                     PersonaAgent(B) + 记忆检索 + 关系引擎(Phase6)
```

技术选型与取舍（为什么自研记忆而不是 Graphiti、为什么 SQLite 而不是图数据库）见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 配置速览

复制 `config.example.yaml` 为 `config.yaml` 后按需修改：双方显示名、聊天记录路径、
LLM 提供商与模型、表情包目录、模拟参数等。**密钥只放环境变量**（`LLM_API_KEY`，
可用配置改名），任何 OpenAI 兼容端点均可（DeepSeek / GLM / 本地推理等）。

## 使用前必读

- [DISCLAIMER.md](DISCLAIMER.md) —— 非心理治疗、非预测、生成内容不代表真实他人意愿；
  如果你正处于情绪危机，请使用文档内列出的求助资源。
- [PRIVACY.md](PRIVACY.md) —— 数据流、脱敏边界、LLM 发送范围。
- **严禁**将本工具用于跟踪、骚扰或监控真实他人；你须对导入数据的合法性负责。

## 路线图

- [ ] v0.2 导入格式扩展（WeChatMsg 等）
- [ ] v0.3 研究指标脚本开放（敏感性/打分/评测——本次为控制范围未随 v0.1 发布）
- [ ] 人格档案手动编辑器（Web 表单）
- [ ] 关系五维状态可视化面板（原研究模块产品化）
- [ ] 完整版分析流水线（v0.1 为简化版：启发式/单轮 LLM 抽取）

## License

[MIT](LICENSE) + 附加条款（禁止用于跟踪/骚扰/监控真实他人）。

---

<div align="center">

「我们回不到过去，但可以练习怎么说那句话。」

</div>
