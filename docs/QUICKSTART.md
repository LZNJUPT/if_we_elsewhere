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

## 2.1 界面全流程（不用命令行）

首次打开（没有 `config.yaml`、也还没有消息）会看到**引导卡**，三选一：

| 选项 | 会发生什么 |
| --- | --- |
| 体验示例数据 | 用内置虚构对话建一个「示例好友（虚构数据）」，并在本机跑完离线分析 |
| 导入我的记录 | 打开导入向导（上传 → 预览确认 → 结果），第三页可直接「立即分析」 |
| 配置 LLM | 打开设置面板，填 provider / base_url / model / key，可一键测试连接 |

日常操作：

- **分析**（顶栏）：勾选「离线分析」＝不调用 LLM（零 token，结果较粗）；不勾＝
  LLM 逐会话抽取事件并生成人格档案。点「开始分析」后会显示阶段进度
  （准备 → 事件 → 记忆 → 关系状态 → 转折点 → 人格档案）与已用时长，可随时「中止分析」。
  分析进行中，「导入记录 / 分析 / 推进一天」会置灰并说明原因。
- **人物档案**（顶栏）：只读查看分析产物——数据概览、双人 L/M/S/U 四层档案、
  关系五维当前值与近 14 个月趋势、转折点列表。人格档案为空模板时会提示去改哪个文件。
- **设置**（顶栏）：改完点「保存」（或「保存并测试连接」立刻验证连通性）。
  密钥保存在 **Windows 凭据管理器**（不可用时回退当前用户 DPAPI 加密文件），
  **不会写进 `config.yaml`**；「清除密钥」后发消息会回到「缺少 LLM API Key」提示。

## 2.2 多个好友（完全隔离）

左侧「好友」栏可以新建 / 切换 / 重命名 / 删除好友。每个好友一个独立数据目录：

```
data/profiles.json            # 注册表
data/profiles/<id>/ifwe_v1.db # 该好友的库（含分析产物与对话线）
data/profiles/<id>/persona/   # 该好友的人格档案
data/profiles/<id>/profile.yaml   # 可选：只覆盖这个好友的配置（如表情包目录）
```

切换好友会一起切换「库 + 人格档案 + 对话线 + 表情包目录」，互不可见。
分析中或导入中不能切换 / 删除好友（会提示原因）。

> 老用户升级：首次启动会把现有 `data/` 内容自动迁到 `data/profiles/default/`，
> 迁移前整目录备份到 `data_backup_<日期>/`（已 gitignore），失败会自动回滚。
> 用 `IFWE_DATA_DIR` 指定过数据目录的场景不参与迁移。

命令行等价物：

```bash
python run.py profile list                  # 看好友列表（* = 当前）
python run.py profile new 小林              # 新建
python run.py profile use xiaolin           # 切换（之后 server/analyze 都用它）
python run.py profile rm xiaolin            # 删除（含数据目录）
python run.py key set                       # 交互式输入密钥（不回显）
```

## 3. 配置 LLM（解锁对话生成）

对话推演由 LLM 驱动。默认 DeepSeek，任意 OpenAI 兼容端点均可：

```bash
# 方式一：界面「设置」面板（推荐给桌面使用）——填 provider/base_url/model/key，
#         点「保存并测试连接」；密钥存进 Windows 凭据管理器，不落明文

# 方式二：环境变量（优先级最高；config llm.api_key_env 可改名）
export LLM_API_KEY=sk-...            # Windows PowerShell: $env:LLM_API_KEY="sk-..."

# 方式三：改 config.yaml 的 llm 段（base_url/model/provider），密钥仍走上面两者之一
```

> 命令行等价物：`python run.py settings --model deepseek-chat`、`python run.py key set`

> 没有 API Key 也能浏览界面、看时间轴与真实记录回放，还能跑「离线分析」；
> 发消息时会得到明确提示。

## 4. 导入你自己的数据

```bash
python run.py init          # 生成 config.yaml（首次运行）
python run.py init --source 你的记录.jsonl \
    --sender-a "导出文件里你的账号名" \
    --sender-b "导出文件里对方的账号名"
python run.py analyze       # 生成事件/记忆/关系状态/转折点/人格（需要 API Key）
# 无 API Key：python run.py analyze --skip-llm（启发式事件 + 手写 persona 模板）
```

带参导入成功后，`chat.source` 与 `people.A.match / people.B.match` 会**自动写回
config.yaml**。之后数据更新了，只需再跑一次 `python run.py init --source 新文件.jsonl`
（账号映射复用配置，不必每次都带 `--sender-a/--sender-b`），或直接
`python scripts/import_chat.py`（完全按 config.yaml 的 source/match 导入）。

格式说明见 [IMPORT.md](IMPORT.md)。

### 4.1 手写 persona（`--skip-llm` 线）

`analyze --skip-llm` 走离线路径，生成的是**空模板**：
`data/persona/persona_v1_A.json` 与 `persona_v1_B.json`。界面/时间轴里的名字
固定显示「你」和「TA」，但**对话个性完全由这两个文件决定**——不填的话数字人格
会很「呆」（demo 里的示例人物有个性，是因为装载了 `sample_data/` 下的完整范例）。

每个文件按 L/M/S/U 四层填写，每条格式 `{"label": "…", "item": "≤40 字"}`，
每层 2~5 条；`label` 客观可验证的写【事实】，主观推断的写【推断】：

| 字段 / 层 | 填什么 |
| --- | --- |
| `display_name` | 对话里 LLM 认知的名字；留空则回退 `config.yaml` 的 `people.B.display`（默认「TA」） |
| `layers.L` 语言风格 | 用词习惯、句长、标点/表情习惯、口头禅 |
| `layers.M` 压力与意义 | 近期在忙什么、压力源、在意什么 |
| `layers.S` 情绪与冲突 | 情绪起伏规律、冲突时的典型反应与修复方式 |
| `layers.U` 自我认知 | 本人认可的自我描述/底线（最优先遵守）；拿不准可留空 |

> 填写范例：`sample_data/persona_v1_A.json` / `persona_v1_B.json`，可对照着写。

生效方式：**编辑后不需要重跑 analyze**——新开一条对话线（或重启
`python run.py server`）就会读取新档案。另外，重跑 `analyze --skip-llm`
**不会覆盖**已填写的档案；想重置模板请删除对应 persona 文件后再跑。

## 5. 日常启动

```bash
python run.py server        # http://127.0.0.1:8015（仅本机可访问）
python run.py desktop       # 桌面窗口（双击式体验；需 pip install pywebview）
python run.py server --profile xiaolin   # 指定好友启动
```

桌面窗口（`desktop.py` / `IfWe.exe`）的额外行为：端口被占用自动换口；
重复打开只会有第一个实例（再点会把已运行的地址用浏览器打开）；
关窗即优雅退出，不留孤儿进程。打包见 README「打包发布物」一节。

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
`python run.py server --port 8088`，或改 `config.yaml` 的 `defaults.port`；
桌面窗口会自动向上找空闲端口。

**Q: 数据库在哪？怎么备份？**
当前好友的 `data/profiles/<id>/ifwe_v1.db`（未启用多好友时是 `data/ifwe_v1.db`，
demo 是 `data_demo/ifwe_v1.db`）。备份 = 复制这个文件（连同同目录的 `persona/`）。

**Q: 密钥到底存在哪里？**
环境变量（如果有）优先；否则界面保存的密钥会写进 Windows 凭据管理器
（服务名 `IfWe`，账号是 `llm.api_key_env` 的值）；凭据管理器不可用时回退
`data/.secret_llm_key`（DPAPI 加密，只有当前 Windows 用户能解）。
`config.yaml` 与日志里都不会出现明文。

**Q: 想推倒重来？**
删掉当前好友的数据目录后重新 `init`（源 JSONL 不受影响，随时可重导入）。

**Q: 界面上的「离线分析」和完整分析差在哪？**
离线：关键词启发式判定事件 + persona 空模板（零 token，适合先看结构）。
完整：LLM 逐会话抽取事件并生成四层人格档案（会消耗 token，只发送脱敏文本）。
