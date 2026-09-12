# IMPORT · 数据导入指南（数据规范 v1）

## 合规边界（先读这个）

- 本项目**不解析微信数据库、不提供任何抓取功能**，也不教程化「怎么导出」。
- 你只能导入**你自己合法导出**的聊天记录文件，并对其合法性负责。
- 导入即脱敏：手机号/身份证/银行卡/车牌/邮箱/地址等模式会被替换为
  `[手机号]`、`[地址]` 之类的占位符，脱敏文本（`content_clean`）才是后续
  分析与 LLM 的唯一输入。

## v1 支持的格式：chatlab JSONL（WeFlow 导出）

每行一个 JSON 对象，形如：

```json
{"_type":"message","platformMessageId":"xxx","timestamp":1751788200,
 "type":0,"content":"今天好累啊","accountName":"你的昵称"}
```

| 字段 | 说明 |
|---|---|
| `_type` | 必须为 `"message"`（其他行会被跳过） |
| `timestamp` | 秒级 Unix 时间戳 |
| `type` | 消息类型：`0` 文本 / `7` 图片或表情 / `4` 文件 / `23` 通话 / `24` 小程序 / `25` 引用文本 / `27` 名片 / `80` 撤回 / `99` 转账 |
| `content` | 内容（表情包为文件名，转账为带金额的文本） |
| `accountName` | 发送者账号名——用 `--sender-a/--sender-b` 映射为 A/B 代号 |

> WeFlow（微信聊天记录本地导出工具）导出的 JSONL 与此格式一致。
> 其他工具（如 WeChatMsg）的兼容在路线图（v0.2）。

## 导入步骤

```bash
# 方式一：一条龙
python run.py init --source data/raw/chat.jsonl \
    --sender-a "你的账号名" --sender-b "对方账号名"

# 方式二：分步
python run.py init                                    # 只生成 config.yaml
python scripts/import_chat.py --source data/raw/chat.jsonl \
    --sender-a "你的账号名" --sender-b "对方账号名"
```

也可以把账号名映射写进 `config.yaml`（之后命令行可省略）：

```yaml
people:
  A: { key: "A", display: "你", match: "你的账号名" }
  B: { key: "B", display: "TA", match: "对方账号名" }
chat:
  source: "data/raw/chat.jsonl"
```

> 约定：`--sender-a` 是**你本人**（对话里的「你」，推演中由你亲自输入），
> `--sender-b` 是对方（数字人格一侧）。账号名必须与导出文件里的
> `accountName` 逐字一致（多个账号名用多次导入/联系作者扩展词表？——v1
> 单名映射，双账号请先合并导出）。

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
