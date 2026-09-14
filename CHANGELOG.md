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

### 导入链路 v2：多来源合并 · 多格式 · 媒体独立通道

主题：**把「准备聊天数据」从「一次一份文件」变成「多次导入、自动按时间合并」**。

- **多份来源一起导入（P0）**：上传支持多文件（单批 ≤12 份、可混合格式），
  逐份体检 + **逐源各自映射 A/B**——不同应用的账号名不同（WeChat wxid /
  Telegram `from_id` / txt 里的昵称），一套名字套所有来源必然有一半映射不上。
- **按时间自动合并**：全部来源归一为 canonical 流后按时间戳全局排序、重算会话切分；
  两份记录在时间上接续的部分自然连成一条连续时间轴（排序/会话化算法本就是全局的，
  新增的是上游「多文件 → 一份统一流」这一层）。
- **跨源去重 + 新门禁 `G8_跨源重复`**：重叠时段里的同一句话只保留一条
  （去重键 = 时间 + 发送者 + 内容哈希；无确定性标识的消息不去重，不猜）；
  门禁报出去重条数与按来源的分布。
- **来源留档 + 「从全部已登记来源重建」**：新增 `app/import_sources.py` 与
  `import_sources` 表。库永远是全部已登记来源重建出来的结果，所以**追加新来源不会
  丢掉旧数据**；界面可查看、单独移除任一来源（移除即按剩余来源重建）。
- **映射一致性自查**：A/B 选反会让去重静默失效（发送者不同 → 去重键不同 → 消息翻倍），
  现会显式告警「N 组消息『时间+内容』相同但发送者相反」，命令行与网页都会出现。
- **新格式：txt / md / log（`plaintext`）+ Word `.docx`**：docx 用标准库
  `zipfile` + `xml.etree` 直读 `word/document.xml`（段落与「时间｜发送者｜内容」表格都收），
  **零新增依赖**。纯文本按三条纪律解析：只认有年份的绝对时间（不猜「昨天」）、
  无时间戳的行按「上一条的续行」并入、无法对应类型的媒体行跳过并计数；
  编码按 BOM → UTF-8 → GB18030 探测（中文导出常见 GBK 系）。
- **PDF 只识别不解析**：体检认出 PDF 后给出「转存为 txt / docx」的可执行引导，
  不引第三方 PDF 库（纯标准库抽不出可靠文本）。
- **媒体独立导入通道**：新增 `app/media_store.py` + `media` / `message_media` 表 +
  媒体库界面（拖拽多张图片，或直接填本机目录批量导入整个表情包文件夹）；
  按内容哈希去重，消息↔媒体按文件名精确匹配关联。`/api/media/{name}` 从
  「只认 32 位 hex 文件名」改为「先查媒体索引，再回退旧 `media.emojis_dir` 约定」，
  对话引擎挑表情包也改为查索引表（自有文件名不再被拒之门外）。
- **`POST /api/import/cancel`**：放弃当前批次并释放导入锁。向导的「← 重新上传」、
  关闭弹窗、以及每次打开向导都会调用 —— 否则用户上传后直接关窗，服务端的批次锁会
  悬着（原本要等 1 小时过期），期间所有上传都报 409。
- **批次锁不再可能把用户挡在门外**（v2 导入实测暴露）：上传时若服务端还挂着上一个
  没走完的批次（关向导 / 刷新页面 / 预览面板重载都会留下它），**新上传直接接管回收**，
  返回 `reclaimed: true`，而不是回 409 让人干等；预览请求自身异常时也会释放锁
  （单个文件认不出来仍保留锁 —— 那是让用户点「移除本份」的可操作状态）。
  前端另加一层：遇到 409 自动放锁重试一次；预览/合并/上传/提交全部带客户端超时，
  任何一步卡住界面都会自己恢复，不会永远停在「处理中…」。
- **数据规范 v2**：`messages` 增 `source_id`/`dedup_key`；新增
  `import_sources` / `media` / `message_media` 表；`meta.spec_version` → `2`；
  旧库首次访问幂等升级（`phase1_ingest.ensure_v2_schema()` 补列 + 建索引），
  无需手动迁移。
- **A/B 映射不再靠"猜"**：导入向导的默认映射从「消息较多的一方 = A」改为
  **config.yaml 里 people.A/B.match 登记过的账号名优先**（支持列表，可登记多个来源的
  账号名），只有没登记时才退回统计猜测，并在界面上标明默认值来源；每份来源下方还会
  并排显示当前选定的 A / B 各自的**脱敏样例**，让用户靠「这是不是我说话的样子」核对。
  提交成功后把本次的账号名记回 config.yaml（最小替换，注释保留），下次导入自动预选。
  起因：默认值把 A 猜成对方时，整份来源的说话人会颠倒（关系状态/人格档案/推演语料
  全跟着错），而多份来源**一致**猜反时 `map_conflicts` 抓不到。
- **`POST /api/import/sources/{id}/mapping` + 来源列表里的「对调 A/B」**：
  修正一份已导入来源的 A/B 映射并立刻按全部来源重建。来源有存档，所以**不需要重新上传**
  ——这正是留档设计的价值。默认动作是按当前值取反（对调），也可显式指定两个账号名。
- CLI 同步支持多来源：`--source` / `--sender-a` / `--sender-b` 均可重复给出，
  新增 `--no-archive`；账号名可从 `config.yaml` 的 `people.A/B.match` 列表读取。
- 测试从 58 个增至 **87 个**（新增纯文本/Word 适配器、v2 结构升级、
  多源合并与去重、映射冲突、媒体存取与关联、同秒消息不丢数据等回归）。

### 变更

- Web 导入向导第二页从「单份预览 + 一组 A/B」改为「**合并预览 + 逐份来源卡片**」：
  每份来源单独选「你 / 对方」，实时显示合并后条数、去重条数、合并时间轴与来源重叠区；
  认不出来的那份可以「移除本份」，其余照常导入。
- 导入重建提示明确「人格档案与媒体库保留」（原先只说 persona 保留）。
- `docs/IMPORT.md` 按数据规范 v2 重写：多来源合并、五种格式、媒体通道与红线、
  门禁表、v1→v2 变更清单。
- 数据目录解析改为「好友 profile 优先、其次全局配置」；`data_dir()/persona_dir()/cache_dir()/
  db_path()` 在切换好友后即时生效（`phase5_common.refresh_paths()` 同步刷新下游缓存）。
- `phase2_llm` 的 base_url/model 改为每次构造客户端时实时读取，
  设置面板改完无需重启。
- 导入临时目录随当前好友切换；导入向导第三页直接给出「立即分析」入口。
- 打包（PyInstaller）时数据目录相对 exe 同级，只读资源从 `_MEIPASS` 读取。
- `scripts/check_privacy.py` 增加目录名前缀跳过（`data_backup_*`，迁移备份里的真实数据不参与发布扫描）。

### 修复（打包链路实测发现）

- **「对调 A/B」连点两次静默失效**：实现写成了固定赋值 `a→B, b→A`，而不是按当前值取反，
  于是第二次点击等于重写同一个结果（用户看到的是「点了没反应」）。改为严格取反，
  连续对调两次必然回到原点。测试抓到的（`test_swap_mapping_fixes_reversed_source`）。
- **前端发送的来源映射服务端读不到（界面实测：导入必定失败）**：界面构造的载荷是
  `{import_id, a, b}`，而 `ImportSourceSel` 声明的是 `sender_a/sender_b`；pydantic
  默认**静默忽略未知字段**，于是每份来源的映射都被读成空串，提交时报
  「「telegram_result.json」还没选 A/B 双方账号」（`/api/import/merge` 同理，
  所以改选 A/B 后的合并预览也一直在失败）。修法：
  ① 前端改为按服务端字段名构造载荷（抽出 `selPayload()`，并在提交前逐份校验、把
  出问题的那份**文件名**报出来）；② 服务端用 `AliasChoices` 同时接受
  `sender_a|a`、`sender_b|b`，并给这些 body 模型加 `extra="forbid"` —— 字段名再对不上
  会直接 422，而不是退化成空串。
  同时补了 **7 条接口契约测试**：断言的是**界面真实发送的形状**（之前只按服务端自己的
  命名发请求，所以完全没覆盖到），含"未知字段必须 422""残留锁回收""预览出错放锁"。
- **新建好友的库缺 v2 两列（`apply_all_schemas` 顺序错）**：v2 升级把
  `ensure_v2_schema()` 放在应用 DDL **之前**，但全新库里那一刻 `messages` 还不存在
  → 函数因「表不存在」直接返回 → 随后 `schema_v1.sql` 建出 v1 版 messages，两列
  永远补不上（只有走导入的 `build_db()` 才正确，所以端到端测试全没覆盖到）。
  现改为 DDL 之后再补列，并补了一条内存库回归用例。旧库在下次访问时自动补齐。
- **单实例检测在中文机器上完全失效（`desktop.py`）**：`tasklist` 的输出是 Windows 本地
  代码页（中文机 GBK），而 `subprocess.run(text=True)` 在 UTF-8 模式下按 UTF-8 解码 →
  `UnicodeDecodeError`。异常发生在**读取线程**里，主调用只拿到空 stdout，于是
  `_pid_alive()` 恒为 `False`：既认不出活着的实例，也不会清理孤儿进程 —— 重复双击会
  起第二个服务（换端口），而不是聚焦已有窗口。新增 `_run_text()` 统一按 bytes 收 +
  `errors="replace"` 解码（只关心 ASCII 的 pid 与进程名）。
- **同秒消息被静默覆盖（数据丢失）**：`message_id` 兜底为 `"<时间戳>-x"`，而没有任何
  适配器产出 `_idx`，于是**没有平台消息 id 的导出**（纯文本、部分 CSV、宽容解析路径）
  里同一秒的多条消息会撞主键，`INSERT OR REPLACE` 直接把前一条覆盖掉。
  实测复现：两条同秒不同内容的消息 id 都是 `100-x`。现改为「时间戳 + 来源内序号」
  并加全局唯一化兜底；多来源合并会把命中率放大，所以是必修项。
- **重建聊天库会连媒体索引一起抹掉**：全量重建走的是删库文件重建，属于独立通道的
  `media` / `message_media` 一并消失（媒体文件还在磁盘上，索引却没了）。
  现重建前先把媒体索引捞出来、建完原样放回，并清理指向已失效消息的旧关联。
- **`/api/media/library` 被 `/api/media/{name}` 抢先匹配**：同名单段路径先注册先匹配，
  `library` 被当成文件名去查图片 → 媒体库列表取不到。媒体库接口改为 `GET /api/media`。
- **双击启动崩溃（用户实测 2026-09-13）**：窗口程序没有标准句柄，`sys.stdout/stderr` 为
  `None`，uvicorn 日志配置调 `sys.stdout.isatty()` 直接 `AttributeError` → exe 起不来。
  现 `config._safe_stdio()` 会安装安全接收端：优先写 `data/logs/desktop.log`
  （用户反馈时可附带，超 512KB 自动重建），失败退回内存黑洞。
- **启动日志句柄挡住首次迁移**：`data/logs/` 属运行期产物，迁移时跳过
  （`logs/`、`tmp_import/`、`.ifwe.lock` 不参与搬迁），否则老用户数据迁不进默认好友。
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
  （成品 exe 会被真的启动一次并访问 `/api/health`、`/api/persona`、`/api/onboarding`、首页；
  冒烟含两种启动方式：重定向输出 + **无标准句柄（等价双击）**）。

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
