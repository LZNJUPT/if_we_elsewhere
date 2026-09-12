# IMPORT · 数据导入指南（数据规范 v1）

## 合规边界（先读这个）

- 本项目**不解析微信数据库、不提供任何抓取功能**，也不教程化「怎么导出」。
- 你只能导入**你自己合法导出**的聊天记录文件，并对其合法性负责。
- 导入即脱敏：手机号/身份证/银行卡/车牌/邮箱/地址等模式会被替换为
  `[手机号]`、`[地址]` 之类的占位符，脱敏文本（`content_clean`）才是后续
  分析与 LLM 的唯一输入。

## 数据从哪来（v0.2）

IfWe **只消费你自己合法导出的聊天记录文件**，不解析任何聊天软件的数据库。
支持的导出来源：

| 格式标识 | 来源工具（官方仓库/文档） | 导出物 | 说明 |
|---|---|---|---|
| `chatlab` | [WeFlow](https://github.com/hicccc77/WeFlow) | JSONL | IfWe 的原生格式，支持最完整 |
| `wecomsg` | [WeChatMsg / MemoTrace 留痕](https://github.com/LC044/WeChatMsg) | CSV | 语音/视频/系统通知等无对应类型的消息会被跳过并计数 |
| `telegram` | [Telegram Desktop](https://telegram.org/blog/export-and-more) 官方导出 | Machine-readable JSON（result.json） | 官方功能导出，无合规争议 |

> 以上链接仅为来源指引，**不构成本项目的使用教程**；导出操作请在各工具官方
> 文档内完成。群聊导出不受支持——只导入双人对话。

## 格式一：chatlab JSONL（WeFlow 导出，原生格式）

每行一个 JSON 对象，形如：

```json
{"_type":"message","platformMessageId":"xxx","timestamp":1751788200,
 "type":0,"content":"今天好累啊","accountName":"你的昵称"}
```

| 字段 | 说明 |
|---|---|
| `_type` | 必须为 `"message"`（其他行会被跳过） |
| `timestamp` | 秒级 Unix 时间戳（v0.2 起也接受毫秒/ISO8601/别名 `ts`/`time`） |
| `type` | 消息类型：`0` 文本 / `7` 图片或表情 / `4` 文件 / `23` 通话 / `24` 小程序 / `25` 引用文本 / `27` 名片 / `80` 撤回 / `99` 转账 |
| `content` | 内容（表情包为文件名，转账为带金额的文本；别名 `text`/`message`） |
| `accountName` | 发送者账号名——用 `--sender-a/--sender-b` 映射为 A/B 代号（别名 `sender`/`talker`/`nick`） |

## 格式二：WeChatMsg（MemoTrace）CSV 导出

选「聊天记录 → CSV」导出（当前稳定版的 `id,MsgSvrID,type_name,is_sender,talker,
room_name,msg,src,CreateTime` 列布局，旧版 `content` 合并列也兼容）。
文本/图片/表情/文件/通话/引用/名片/转账/撤回会入库；语音、视频、系统通知等
canonical 无对应类型的消息**跳过并计数**（不做猜测归类）；群聊行跳过。
账号名通常是 wxid，用「导入体检」或 Web 预览里的候选名单确认后再映射。

## 格式三：Telegram Desktop 官方导出 JSON

Telegram Desktop → Settings → Advanced → Export chat history →
Machine-readable JSON（`result.json`）。文本（含混合数组实体拼接）、图片、
贴纸、文件会入库；语音消息、编辑事件、service 行跳过并计数；
超过两个发送者的行按群聊判定跳过。时间戳带时区偏移按原偏移解析，
裸时间按东八区（与项目 v1 约定一致）。

## 导入体检（doctor，只读不写库）

不确定文件能不能导？先体检：

```bash
python run.py doctor --source 你的记录.csv
```

输出：格式识别结果、有效消息数、时间跨度、双方候选账号及占比、消息类型
分布、脱敏规则预估命中数，以及「可导入 / 缺什么 / 建议参数」的结论。
对无法识别的文件给出原因列表，不会崩溃。

## 导入步骤

```bash
# 方式一：一条龙（--format 默认 auto，会自动探测格式）
python run.py init --source data/raw/chat.jsonl \
    --sender-a "你的账号名" --sender-b "对方账号名"

# 方式二：分步
python run.py init                                    # 只生成 config.yaml
python scripts/import_chat.py --source data/raw/chat.csv \
    --format wecomsg \
    --sender-a "你的账号名" --sender-b "对方账号名"
```

`--format` 可选 `auto`（默认）/ `chatlab` / `wecomsg` / `telegram`。

也可以把账号名映射写进 `config.yaml`（之后命令行可省略）：

```yaml
people:
  A: { key: "A", display: "你", match: "你的账号名" }
  B: { key: "B", display: "TA", match: "对方账号名" }
chat:
  source: "data/raw/chat.jsonl"
```

> 约定：`--sender-a` 是**你本人**（对话里的「你」，推演中由你亲自输入），
> `--sender-b` 是对方（数字人格一侧）。账号名必须与导出文件里的发送者
> 字段逐字一致（WeChatMsg 通常是 wxid，Telegram 是 from_id）。

## Web 导入向导

不想用命令行？启动本地界面（`python run.py server`）后，左侧栏点
「**导入记录**」：

1. **上传**：拖拽或选择 `.jsonl / .json / .csv` 文件（≤50MB）；
2. **预览确认**：格式、跨度、类型分布、脱敏预估 + **A/B 下拉选择**
   （候选账号 + 各自消息数 + 脱敏样例）；
3. **导入结果**：门禁摘要（时间序/双人占比/脱敏残留/未知发送者）。

上传的临时文件在本机 `data/tmp_import/` 处理，导入链路结束（含失败）
立即删除；全程不出本机。详见 [../PRIVACY.md](../PRIVACY.md)。

## 导入后会发生什么

1. **去噪**：统一空白/换行；
2. **脱敏**：命中隐私模式的内容替换为占位符，并统计 `has_privacy`；
3. **会话化**：相邻消息间隔 > 30 分钟视为两个会话（可配置）；
4. **入库**：写入 `data/ifwe_v1.db` 的 `messages / sessions / daily_stats`；
5. **质量门禁**：时间序校验、双人占比（双方各 >25%）、脱敏残留复扫、
   未知发送者/类型检查——任一失败以非零退出码提示。

产出报告：`data/phase1_验收报告.md` 与 `data/phase1_summary.json`。

## 导入之后

```bash
python run.py analyze
```

分析流水线（v0.1 简化版）会生成：

| 产物 | 表/文件 | 说明 |
|---|---|---|
| 事件 | `events` | 会话级事件抽取（LLM 路径；`--skip-llm` 为关键词启发式） |
| 记忆 | `facts` | 双时态事实（episodic），供检索召回 |
| 关系状态 | `relationship_state` | 逐月五维估计量（置信度 0.4，非真值） |
| 转折点 | `turning_points` | 断联窗口（≥14 天）、重要事件 Top5、活跃高峰月 |
| persona | `data/persona/persona_v1_{A,B}.json` | L/M/S/U 四层人格档案（LLM 生成或手写模板） |

## 表情包（可选）

把导出目录里的表情包文件夹路径填进 `config.yaml`：

```yaml
media:
  emojis_dir: "path/to/weflow_emojis"   # 示例：指向你导出的 Emojis 文件夹
```

要求：文件名为 32 位十六进制 + 图片扩展名（WeFlow 导出即如此）。
不配置则界面显示占位符，不影响其他功能。

## 常见问题

**Q: 门禁提示「双人占比」未通过？**
大概率账号名映射错了（A/B 写反或拼写不一致导致一方被记为未知发送者 X）。

**Q: 消息条数与导出工具显示的不一致？**
导入会跳过非 `_type=message` 行与无法解析的行；请以 `phase1_验收报告.md` 的
统计为准，并对照导出工具的过滤条件。

**Q: 想重新导入？**
默认每次导入都会重建数据库（`--no-reset` 除外）；你的源文件只读、永不修改。

**Q: 隐私担心？**
见 [../PRIVACY.md](../PRIVACY.md)：脱敏在入库前完成；外发内容仅限脱敏文本
与派生结论；原始内容只存于你本机的数据库文件。
