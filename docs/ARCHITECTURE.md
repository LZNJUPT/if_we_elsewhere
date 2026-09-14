# ARCHITECTURE · 架构与选型

IfWe 的目标是把「关系回溯 + 反事实推演」做成一个本地可跑、可解释、隐私可控
的产品。本文说明当前架构与关键选型的取舍。

## 分层架构

```
┌────────────────────────────────────────────────────────────────────┐
│ T4 产品层    phase15_web（原生 HTML/CSS/JS 单页）                    │
│              desktop.py（uvicorn + pywebview 原生窗口 / IfWe.exe）    │
│              run.py（init/analyze/server/demo/desktop/profile/key）  │
├────────────────────────────────────────────────────────────────────┤
│ T3 服务层    phase15_api（FastAPI，仅 127.0.0.1）                    │
│              /api/state /api/anchors /api/history /api/recall        │
│              /api/lines /api/say /api/day /api/media                │
│              /api/analyze[/status|/cancel] /api/persona              │
│              /api/settings[/test] /api/profiles[/switch|/rename]     │
│              /api/onboarding[/demo] /api/import/{upload,preview,     │
│              commit}                                                │
├────────────────────────────────────────────────────────────────────┤
│ T2 内核层    DialEngine（对话内核：消息驱动 + 自动跨天）              │
│              PersonaAgent（人格-情绪-媒介抽样-去重护栏）              │
│              MemoryRetriever（SQL+可选向量，双时态 as_of）            │
│              RelEngine（关系五维：事件增量+回声+稳态回归）            │
│              analyze_pipeline（五类产物，带 progress/cancel 回调）    │
├────────────────────────────────────────────────────────────────────┤
│ T1 数据层    SQLite 单文件库（WAL）                                   │
│              主库: messages/sessions/events/facts/relationship_state  │
│              隔离: sim_* 命名空间（推演专用，主库只读）                │
│              profile: 每个好友一个独立数据目录（data/profiles/<id>/）  │
│              密钥: Windows 凭据管理器 / DPAPI 文件（不落明文）         │
└────────────────────────────────────────────────────────────────────┘
```

## 关键设计

### 0. 多好友隔离：为什么是「一个好友一个数据目录」

隔离方案上有两条路：给每张表加 `friend_id` 维度，或让每个好友独占一套数据目录。
本项目选了后者（v0.3），理由是：

- **复用既有机制**：`config.paths.data_dir` 早已支持按目录切换（`IFWE_DATA_DIR`
  在 demo 场景验证过）；把「好友」映射成「数据目录」后，
  `data_dir()/db_path()/persona_dir()/cache_dir()` 无需改动即随好友切换；
- **隔离最彻底**：库、人格档案、表情包目录、检索缓存、对话线全部物理分离，
  不存在「查询漏了 `friend_id` 过滤」这类串扰风险；
- **零迁移成本**：不改表结构、不改查询；老数据整体搬进 `data/profiles/default/` 即可。

代价是跨好友的聚合查询（例如「所有好友的消息总数」）需要逐个目录开库——对一个
单机单人使用的工具来说，这个代价可以接受。

### 1. 双层记忆

- **长期记忆（facts）**：双时态事实表——`valid_at/invalid_at` 表达「什么时候
  成立」，`as_of` 检索保证 Agent 只能看到当时点已知的信息。打分 =
  相关度(IDF 加权 bigram) + 置信度 + 时间衰减 × 记忆强度（检索即强化）。
- **短期缓冲（WorkingMemory）**：字符预算内的近期对话；超限自动把最老一半
  折叠为摘要写回长期记忆。进程重启后从库重建，不丢上下文。
- **融合检索**：主库记忆 + 本分支 `sim_facts` 用 RRF 融合；IF 线分歧点之后的
  真实记忆按分歧点截断（反事实冻结），模型无法「偷看」被改写掉的未来。

### 2. 为什么自研记忆而不是 Graphiti

我们做过 Graphiti（Kuzu 嵌入式图库）的完整 PoC（见 [../poc/README.md](../poc/README.md)）：
时间化知识图谱的表达力很强，但

- 构建成本高（逐会话 LLM 抽取实体/关系）；
- 嵌入式图库的检索与维护复杂度超出「单用户本地工具」的需要；
- 中文语料的实体抽取质量一般，噪声实体反而干扰上下文。

自研方案用「一张双时态 facts 表 + 关键词/向量混合检索」覆盖了 80% 的需求，
换来：零服务依赖、秒级查询、数据结构完全透明可审计。

### 3. 为什么关系状态是「五维 + 解释链」而不是单一分数

关系是多维的：一次争吵会同时推高冲突、削弱亲密与情绪安全，但可能通过事后的
坦诚沟通增加信任。单值评分掩盖这些结构。RelEngine 用规则表把 12 类事件映射为
五维增量（severity/importance 缩放 + 月粒度 tanh 压缩 + 回声衰减），每条状态
变化都能回答「为什么」。

### 4. 模拟隔离（红线设计）

所有推演写入 `sim_*` 前缀表（sim_messages/sim_events/sim_facts/sim_rel_state/…）
与独立分支表 `ifr_branch`；主库在推演路径上是**只读**的。`selftest`
（`python app/phase15_dial_engine.py selftest`）会在离线状态下自动验证
「推演前后主库零变化」。

### 5. 消息驱动的时间推进

对话产品态没有「上帝视角的日程表」：用户每说一句即一个回合，累计消息数达到
阈值（默认 20）自然跨天；也可以手动推进。关系状态按天结算事件（同类型事件
当天只计权一次，第二次衰减，第三次起不计），避免刷分。

### 6. 导入链路 v2：多来源合并的设计取舍

真实场景几乎不是「一次导出一份文件」——同一段对话可能微信那边导过一次、
Telegram 那边也导过一次、后来又有人整理成 txt。所以导入被设计成**可累积**的。
四个关键取舍：

1. **库 = 全部已登记来源重建的结果**（而不是「在已有库上追加写入」）。
   追加写入看起来更省事，但会带来「同一份文件导两次就翻倍」「移除某来源要逆向减
   统计」这类幂等性泥潭。改成「登记来源 → 整体重建」后，语义变成
   *同源同参 → 结果确定*，来源可单独移除、可替换，代价是必须留源存档
   （`import_sources.py` + `sources/`），以及每次导入都要重跑一遍全量解析。
   聊天记录的量级下这个代价可以接受。
2. **逐源映射 A/B**，而不是全局一组账号名。不同应用的账号名天然不同
   （WeChat wxid / Telegram `from_id` / 纯文本里的昵称），用一套名字套所有来源
   必然有一半映射不上，而映射不上的消息会被记为未知发送者并触发 G7 门禁。
3. **跨源去重键 = 时间 + 发送者 + 内容哈希**，且**只对有确定性标识的消息生成**：
   有文本用文本，无文本但有附件名用附件名，两者都没有（如无文件名的 `[图片]`）
   返回 `None` 表示不去重。宁可漏去重也不误删——这是本项目「不猜测归类」原则
   在去重上的延伸。
4. **映射一致性自查**。A/B 选反时去重会**静默失效**（发送者不同 → 去重键不同 →
   同一句话被当成两条），这是最难自己发现的错误。所以去重之后会再扫一遍：
   若同一「时间 + 内容」仍同时出现在 A 与 B 两侧，几乎只有一个解释——某份来源
   选反了。这条判断以 `map_conflicts` 暴露到预览与门禁里，命令行与网页都会告警。

配套的结构变更（数据规范 v1 → v2）：`messages` 增 `source_id`（来源登记 id）与
`dedup_key`；新增 `import_sources`（来源台账）、`media`（媒体索引）、
`message_media`（消息↔媒体关联）。旧库首次访问由
`phase1_ingest.ensure_v2_schema()` 幂等补列建索引，无需手动迁移。

**媒体为什么是另一条通道**：聊天记录里的表情包只是一个文件名，真正的图片文件
往往在另一次导出里、甚至散落在用户自己收集的文件夹里。所以媒体单独导入
（`media_store.py`），按内容哈希存档去重，再靠**文件名精确匹配**与消息关联
（匹配不上留空）。`/api/media/{name}` 因此从「只认 32 位 hex 文件名」改为
「先查媒体索引，再回退旧 `media.emojis_dir` 约定」。
注意媒体**不做脱敏**（原始二进制），也**不参与任何分析或 LLM 调用**。

## 目录结构

```
if_we_elsewhere/
├── run.py                  # 一键入口（init/analyze/server/demo）
├── config.example.yaml     # 配置模板（复制为 config.yaml）
├── app/
│   ├── config.py           # 配置加载器（env > yaml > 默认值）
│   ├── phase1_ingest.py    # 导入/脱敏/会话化/门禁（库）+ v2 多源合并（_assemble/plan_merge/ingest_many）
│   ├── importers/          # 格式适配器（插件式，按注册序自动探测）
│   │   ├── chatlab_jsonl.py    # WeFlow JSONL / 通用 CSV
│   │   ├── wecomsg_csv.py      # WeChatMsg(MemoTrace) CSV
│   │   ├── telegram_json.py    # Telegram Desktop 官方导出 JSON
│   │   ├── docx_text.py        # Word .docx（标准库 zipfile+ElementTree，零依赖）
│   │   └── plaintext_lines.py  # txt/md/log 行流启发式（docx 复用其解析）
│   ├── import_sources.py   # 已导入来源登记 + 源存档（库由全部来源重建）
│   ├── media_store.py      # 媒体（表情包/图片）存档、索引与消息关联
│   ├── doctor.py           # 导入体检（只读，不写库）
│   ├── phase2_llm.py       # LLM 结构化输出鲁棒层（json_object+修复+重试）
│   ├── phase4_retrieval.py # 记忆检索（bigram+IDF+可选向量, 双时态, 遗忘/强化）
│   ├── phase5_common.py    # 模拟时钟/世界状态/分支记忆融合
│   ├── phase5_a2_loop.py   # PersonaAgent + 会话循环 + 事件落库
│   ├── phase5_a3_buffer.py # 短期缓冲（摘要折叠）
│   ├── phase5_a5_pcc.py    # 人格一致性校准（规划/反思）
│   ├── phase6_engine.py    # 关系五维引擎（规则表+回声+压缩）
│   ├── phase15_dial_engine.py # 对话内核（你上场 + 数字人格）
│   ├── phase15_api.py      # FastAPI 服务层
│   ├── phase15_web/        # 前端（零构建原生三件套）
│   └── schema*.sql         # 全部 DDL（幂等执行；v1 基础 + v2 增量）
├── scripts/
│   ├── import_chat.py      # 导入 CLI
│   └── check_privacy.py    # 隐私扫描门禁
├── sample_data/            # 纯虚构示例数据
├── poc/                    # 可选 PoC（graphiti 对照实验等）
└── docs/                   # 本文档与更多
```

## 技术栈

- **后端**：Python 标准库 + SQLite + FastAPI/uvicorn + pydantic + openai SDK
  （任意 OpenAI 兼容端点）
- **检索**：纯 SQL（CJK bigram + IDF）为基线；fastembed（本地 ONNX）可选增强
- **前端**：零构建、零 CDN 的原生 HTML/CSS/JS
- **LLM**：json_object 模式 + pydantic 校验 + 字段级类型修复 + 多层重试
  （对偶发空响应/截断/格式漂移的工程化兜底）
