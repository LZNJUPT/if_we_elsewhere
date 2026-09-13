# Changelog

## v0.3.0 (2026-09，草稿)

主题：**从「命令行工具」变成「开箱即用的本地 Windows 应用」**——双击启动、全图形化操作、
多好友隔离，同时不破坏既有隐私设计（CLI 全部保留，GUI 是增量入口）。

### 新增

- **CI 自动出成品（GitHub Actions）**：`.github/workflows/release.yml`
  推送 `vX.Y.Z` 标签 → 校验（测试 + 隐私门禁 + 版本一致性）→ PyInstaller 构建 →
  打包 zip/SHA256 → 成品冒烟 → **创建正式 Release**（标记 Latest）；
  每次推送 `main` 则原地更新 `nightly` 预发布，永远能下到最新构建；PR 只跑校验。
  用户不需要下载源代码，直接下免安装包即可。
- **发布脚本**：`scripts/package_release.py`（打包 zip + SHA256，内置**数据红线闸门**：
  产物里出现 `config.yaml`/`*.db` 即中止；`--verify` 校验 zip 结构）与
  `scripts/release_notes.py`（从 CHANGELOG 生成 Release 说明；`--check` 校验
  tag == `VERSION` == CHANGELOG）。
- **版本号单一来源**：仓库根 `VERSION`，`/api/health` 与桌面启动器都会回显（打包时随包分发）。
- **桌面启动器 `--no-window`**：只起本地服务、不开窗口也不开浏览器，供冒烟测试/自接前端用；
  同时输出机器可读的 `[desktop] READY http://127.0.0.1:<port>`。
- **界面内分析（替代 CLI `analyze`）**：顶栏「分析」→ 勾选是否离线 → 阶段进度面板
  （1 准备 / 2 事件 / 3 记忆 / 4 关系状态 / 5 转折点 / 6 人格档案 + 当前消息 + 已用时），
  支持中途中止；完成后一键跳到人物档案。
  后端：`POST /api/analyze`、`GET /api/analyze/status`、`POST /api/analyze/cancel`；
  `analyze_pipeline.run_all()` 新增 `progress` / `should_cancel` 回调（原有 `[2]~[6]`
  print 与 CLI 输出保持不变）。
- **人物档案（只读）**：`GET /api/persona` + 界面「人物档案」——数据概览、
  双人 L/M/S/U 四层档案（空模板会给填写指引）、关系五维当前值与近 14 个月趋势、
  转折点列表。全部来自已有产物，无新计算。
- **LLM 设置面板**：`GET/PUT /api/settings`、`POST /api/settings/test`；
  provider / base_url / model / max_tokens 定向写回 `config.yaml`（逐行最小替换，注释保留；
  结构对不上才回退整体重写），改完立即生效（刷新 LLM 端点 + 失效引擎缓存）。
- **密钥不落明文**：新增 `app/secret_store.py`——优先 `keyring`（Windows 凭据管理器，
  服务名 `IfWe`），不可用时回退**当前 Windows 用户 DPAPI 加密文件**；
  `GET /api/settings` 只回 `key_hint`（如 `sk-***abc`），任何响应都不含完整 key；
  清除后发消息回到「缺少 LLM API Key」提示。
- **多好友（profile）**：每个好友一个独立数据目录 `data/profiles/<id>/`
  （库 / persona / 缓存 / 对话线全隔离，**零表结构改动**，复用 `IFWE_DATA_DIR` 机制）。
  注册表 `data/profiles.json`；`GET/POST/DELETE /api/profiles` +
  `POST /api/profiles/switch` + `POST /api/profiles/<id>/rename`；
  界面左侧好友栏（新建 / 切换 / 重命名 / 删除 + 当前好友名常显）。
  可选好友级配置 `data/profiles/<id>/profile.yaml`（覆盖全局 config.yaml）。
- **老用户自动迁移**：首次启动把现有 `data/` 内容迁入 `data/profiles/default/`，
  迁移前整目录复制备份到 `data_backup_<date>/`（已 gitignore），
  中途失败自动回滚、原数据不受影响；`IFWE_DATA_DIR` 自定义目录不参与迁移。
- **首次运行引导**：无 config.yaml 且无消息时显示引导卡，三选一：体验示例数据
  （构建虚构示例库并注册为「示例好友（虚构数据）」）、导入记录、配置 LLM。
- **全局互斥**：分析 / 导入 / 好友切换 / 好友删除共用一把互斥锁，进行中一律 409，
  界面据此置灰「导入记录 / 分析 / 推进一天」并给出原因 tooltip。
- **Windows 桌面壳与打包**：新增 `desktop.py`（uvicorn + pywebview 窗口，关窗优雅停服；
  端口占用自动换口；单实例锁 + 孤儿进程清理；无 pywebview / 无 WebView2 时回退默认浏览器）
  与 `IfWe.spec`（onedir、不加壳不用 UPX、静态资源与示例数据打包清单纳入版本管理）。
- **CLI 等价物**：`run.py profile list|new|use|rm|migrate`、`run.py settings`、
  `run.py key set|show|clear`、`run.py desktop`，`server`/`analyze` 新增 `--profile`。
- **测试**：新增 `tests/test_profiles.py`（21 个用例）覆盖迁移与备份/回滚、
  好友 CRUD 与 id 规则、`profile.yaml` 覆盖范围、`data_dir` 跟随切换、
  删除保护、配置定向写回（注释保留）与密钥存储（强制走 DPAPI 回退，
  不触碰真实凭据管理器）；`tests/` 全量 52 用例通过。

### 变更

- 数据目录解析改为「好友 profile 优先、其次全局配置」；`data_dir()/persona_dir()/cache_dir()/
  db_path()` 在切换好友后即时生效（`phase5_common.refresh_paths()` 同步刷新下游缓存）。
- `phase2_llm` 的 base_url/model 改为每次构造客户端时实时读取，
  设置面板改完无需重启。
- 导入临时目录随当前好友切换；导入向导第三页直接给出「立即分析」入口。
- 打包（PyInstaller）时数据目录相对 exe 同级，只读资源从 `_MEIPASS` 读取。
- `scripts/check_privacy.py` 增加目录名前缀跳过（`data_backup_*`，迁移备份里的真实数据不参与发布扫描）。

### 修复（打包链路实测发现）

- **打包后只读资源定位错误**：冻结运行时模块的 `__file__` 指向 `_MEIPASS` 根，而
  schema / 静态前端 / 示例数据按 spec 放在 `_MEIPASS/app` 下 → 打包版会「导入记录」直接失败、
  人物档案 500。新增 `config.resource_dir()/sample_dir()/template_path()` 统一探测，
  `phase1_ingest`（建库 schema）、`phase5_common`、`analyze_pipeline`、`phase15_dial_engine`、
  `phase15_api` 全部改用（源码运行时行为不变）。
- **新库 / 空数据目录缺表**：`relationship_state` 等表只在 analyze 时才建，
  导致 `selftest` 在空目录直接崩、全新好友点「人物档案」报 500。现由
  `phase5_common.apply_all_schemas()` 建齐全部结构（API 首次访问某个库时执行一次，
  导入重建库后自动失效重跑；CLI 入口同样改用），`pc.connect()` 也会自动创建父目录。
- 打包版 `data/` 首次启动迁移、`nightly` 与正式版的版本号一致性均由 CI 与冒烟步骤覆盖
  （成品 exe 会被真的启动一次并访问 `/api/health`、`/api/persona`、`/api/onboarding`、首页）。

### 明确未做（本期非目标）

- 云同步 / 多设备 / 远程访问（仍只监听 127.0.0.1）；多用户账号体系（仍是单机单用户 + 多好友档案）。
- 前端框架化重构（保持原生 HTML/CSS/JS、零构建）；自动更新器。
- 人格档案**编辑器**（本期为只读展示，填写仍走 persona JSON）。

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

### 修复（新用户验证反馈）

- **`init` 带参导入成功后回填 config.yaml**：`chat.source` 与
  `people.A.match / people.B.match` 自动写入（模板文本最小替换保留注释，
  结构对不上时回退 YAML 重写）；`scripts/import_chat.py` 同样接入。
  此前仅复制模板，映射用完即弃，二次导入只带 `--source` 会报
  「缺少账号名映射」（BUG_REPORT_NEWUSER_2026-09-12 Bug #1）。
- **persona `display_name` 为空串时回退失效**：`--skip-llm` 空模板的
  `""` 会让提示词出现 `你是「」`；现按空值处理，回退 config
  `people.{A,B}.display`（BUG_REPORT_NEWUSER_2026-09-12 Issue #2）。
- **重跑 `analyze --skip-llm` 不再覆盖手写 persona**：非空档案跳过模板
  写入并提示；`analyze` 完成时空模板会打印填写指引（模板路径 / 范例 /
  生效方式：新开对话线或重启 server，无需重跑分析）。
- 文档：QUICKSTART §4 增补「配置自动写回」说明与 §4.1 手写 persona
  指引（各层填法、范例位置、生效机制）。

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
