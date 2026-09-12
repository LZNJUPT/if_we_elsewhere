# Changelog

## v0.2.0 (2026-09，草稿)

主题：**大幅降低"准备聊天数据"的门槛**。导入器插件架构 + 两个新来源适配 +
导入体检 + Web 导入向导。

### 新增

- **导入器插件架构**：`app/importers/` 统一 Importer 协议（detect/parse →
  canonical 消息）；`ingest()` 支持自动格式探测（未识别回退 chatlab）；
  `import_chat.py` 新增 `--format auto|chatlab|wecomsg|telegram`。
- **WeChatMsg（MemoTrace / 留痕）CSV 适配**：当前稳定版 CSV 列布局实测映射；
  语音/视频/系统通知等无 canonical 对应类型的消息跳过并计数（不猜测归类）；
  群聊行跳过。
- **Telegram Desktop 官方导出 JSON 适配**：`result.json` 的混合数组文本拼接、
  附件映射、service/编辑事件跳过计数、双人判定（>2 发送者按群聊处理）。
- **导入体检 `run.py doctor --source <file>`**：只读不写库；输出格式识别、
  有效消息数、时间跨度、双方候选账号占比、类型分布、脱敏预估与建议参数；
  无法识别的文件输出原因列表，任何输入都不崩溃。
- **字段宽容解析**（chatlab 适配器内）：字段别名表（timestamp/ts/time、
  content/text/message、accountName/sender/talker/nick）；时间戳单位自动
  识别（>1e12 毫秒、1e9~1e12 秒、ISO8601 含 Z/偏移）；通用 CSV 支持
  （utf-8-sig → utf-8 → gbk 编码探测）。
- **Web 导入向导**：左侧栏「导入记录」三步向导（上传 → 预览确认 → 结果）；
  预览含 A/B 候选账号下拉（计数 + 脱敏样例）；提交后返回质量门禁摘要。
  后端三端点（upload/preview/commit）+ 文件锁并发保护 + 临时文件
  链路结束即删（含失败路径），全程本机。
- **测试**：`tests/`（stdlib unittest）覆盖三个适配器的固定样本快照、
  别名/单位规则、跳过计数、doctor 与 Web 链路冒烟；样本全部为合成虚构数据。

### 变更

- `phase1_ingest.load_raw` 迁入 `importers/chatlab_jsonl`（行为不变，原函数
  保留为兼容薄壳）；同一输入的导入结果与 v0.1 逐字段一致（对拍验证）。
- 文档：IMPORT（中/EN）重写「数据从哪来」（格式表 + 外部工具官方链接，
  只链接不教程化）；PRIVACY（中/EN）增补 Web 导入数据流与临时文件生命周期。

### 明确未做（立场声明）

- **未内置任何 IM 的数据库解析**：本项目不接触微信/Telegram 等客户端的
  数据库、进程内存或备份文件，只消费用户已合法导出的文件；
- 文档只链接外部工具官方页，不写任何导出教程；
- 语音/视频消息暂无 canonical 表示，跳过并计数（不做猜测归类）。

## v0.1.0 (2026-09)

首个公开版本。小而干净的主线：导入 → 分析 → 对话推演。

### 新增

- **一键入口 `run.py`**：`init`（配置+导入）/ `analyze`（分析）/ `server`
  （本地界面）/ `demo`（虚构示例数据一键体验）。
- **导入流水线**：chatlab/WeFlow JSONL → 规范 SQLite 库；导入即脱敏
  （手机号/地址/证件号/银行卡/车牌/邮箱）；结构性质量门禁（双人占比、
  脱敏残留、未知发送者等）。
- **分析流水线 v0.1**（简化版）：
  - 会话级事件抽取（LLM 路径 / `--skip-llm` 关键词启发式）；
  - 双时态记忆 facts（episodic）；
  - 逐月关系五维估计量（事件增量 + 回声 + 基线回归）；
  - 转折点检测（断联窗口 / 重要事件 / 活跃高峰）；
  - persona 生成（LLM 生成或手写模板，L/M/S/U 四层）。
- **对话推演内核（DialEngine）**：消息驱动 + 自动跨天 + 短期缓冲 + 反事实
  冻结 + 真实表情包加权复现；`sim_*` 模拟隔离，主库零写入（离线自检验证）。
- **本地 Web 界面**：对话窗、时间轴选点（月度消息量/五维状态/转折点）、
  历史记录回放与「想起来了吗」回忆预览、表情包渲染。
- **隐私门禁 `scripts/check_privacy.py`**：红线词表扫描，命中即非零退出，
  可挂 pre-commit/CI。
- **配置化**：昵称/路径/日期/模型/端口/种子全部进入 `config.yaml` +
  环境变量覆盖；密钥只走环境变量。
- **文档**：双语（中/EN）README、PRIVACY、DISCLAIMER、QUICKSTART、IMPORT、
  ARCHITECTURE、ETHICS；LICENSE = MIT + 附加条款（禁止跟踪/骚扰/监控真实
  他人）。
- **示例数据**：79 条纯虚构双人对话 + 虚构 persona（与任何真实人物无关）。

### 明确未包含（路线图）

- 研究指标脚本（敏感性/打分/评测、叙事注入实验等）
- WeChatMsg 等其他导出格式兼容
- 完整版分析流水线（v0.1 为简化版）

### 隐私声明

本发布树在构建过程中全程只读源项目；经 `check_privacy.py` 红线词表全量扫描
零命中后发布。示例数据为纯虚构创作。
