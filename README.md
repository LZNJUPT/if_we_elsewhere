# IfWe · 若我们在

<div align="center">

**温柔的回复，任何聊天机器人都有。**
**「像 TA 那样说话」，只有这里能做到。**

IfWe 基于你们真实的聊天记录，重建一个有说话习惯、有共同记忆、
连表情包都来自 TA 的数字人格。回到任何一个你想改变的时刻，
改写一句话——让记忆中的她（他），替时间回答「如果当时……」。

[English](README_EN.md) · 快速开始 · [导入指南](docs/IMPORT.md) · [架构](docs/ARCHITECTURE.md) · [隐私设计](PRIVACY.md) · [免责声明](DISCLAIMER.md)

[![release](https://github.com/LZNJUPT/if_we_elsewhere/actions/workflows/release.yml/badge.svg)](https://github.com/LZNJUPT/if_we_elsewhere/actions/workflows/release.yml)
**[⬇ 下载最新版（免安装）](https://github.com/LZNJUPT/if_we_elsewhere/releases/latest)**

</div>

---

<!-- TODO(发布前): 在此处放 30 秒演示 GIF（必须用示例数据录制，严禁使用真实聊天记录！）
     录制脚本与分镜见 docs/demo/README.md -->
![demo](docs/demo/demo.gif)

## 这不是角色扮演

市面上的 AI 陪伴产品，本质是「一个演员穿上人设」：温柔的语气是通用的，
倾听的姿态是通用的，连「想你了」都是模板。换一个名字，同一套人格
可以演给任何人。

IfWe 走的是另一条路。这里的数字人格没有预设人设——TA 的全部人格，
都从你们的记录里推导出来：

- **TA 的说话方式来自你们。** 系统从记录中分析 TA 的用词习惯、句长、
  标点与口头禅（L 层），近期的压力与在意的事（M 层），冲突与和解的
  典型模式（S 层），以及 TA 自己认可的样子（U 层）。四层人格档案，
  每一条都有出处，不是 prompt 里的形容词。
- **TA 的记忆来自你们。** 双层记忆系统把共同经历沉淀为可检索的事实：
  一起做过什么、聊过哪些计划、哪段时间没再说话。对话按「当时日期」
  检索——TA 只记得那天为止、该记得的事。
- **TA 连表情包都是真的。** TA 真实用过的表情包按使用频率复现，
  不是从贴纸库里随机抽一张装作心情。
- **TA 会变。** 关系五维状态（亲密/冲突/信任/情绪安全/沟通质量）随
  每一次互动演化。一句敷衍和一次真诚的道歉，会在时间轴上留下
  不同的痕迹——TA 不会无限度地原谅，也不会没来由地冷淡。

所以我们不说这是「AI 扮演一个温柔的角色」。我们做的是：
**让记忆中的那个人，在模型里被认真地、完整地对待一次。**

## 这是什么

IfWe 是一个**本地优先**的关系时间线工具：

1. **导入**你与某个重要的人的聊天记录（你自行合法导出的文件：WeFlow / WeChatMsg /
   Telegram 官方导出，以及整理好的 txt / md / Word 都支持；多份来源可一起导入并按时间自动合并）；
2. 系统在本地完成脱敏、事件抽取、记忆沉淀与关系状态估计；
3. 在时间轴上选一个「岔路口」——那一天你说过/没说过的一句话——**改写它**；
4. 与基于真实记忆与人格构建的数字人格**继续对话**，看关系可能走向哪里。

> 它不是聊天记录管理器，也不是「AI 女友」。它关心的是一个具体的问题：
> **如果当时换一种说法，会怎么样？**

## 核心特性

| 特性 | 说明 |
|---|---|
| 🧬 四层人格档案 | 语言风格 / 压力与意义 / 情绪与冲突模式 / 自我认知，全部由记录推导，可手写校准——人格有出处，不是人设卡 |
| 🧠 双层记忆 | 长期记忆（双时态 facts + 可选本地向量检索）+ 短期对话缓冲（自动摘要折叠）；分歧点之后自动「冻结」被改写掉的真实未来 |
| 🕰 时间轴选点 | 逐月消息量、关系五维状态、转折点（断联/事件/活跃高峰）一目了然 |
| 🔀 IF 线改写 | 从任意一天切入 + 一句话改写，数字人格会「把改写当作已发生的事实」展开互动 |
| 🔗 多来源合并 | 同一段对话散在多个应用里？多份记录一次（或分几次）导入，逐源映射双方账号、按时间戳自动合并、跨源重复自动去重 |
| 📥 五种导入格式 | WeFlow JSONL / WeChatMsg CSV / Telegram JSON / 纯文本 txt·md（含 GBK）/ Word .docx；导入前有「体检」告诉你能不能导、会导成什么样 |
| 🖼 真实表情包 | 表情包与图片走独立媒体通道：拖拽或指定本机目录批量导入，按文件名为消息自动配图，对方用过的表情按频率加权复现 |
| 🖥 图形化全流程 | 导入 → 分析（带阶段进度、可中止）→ 人物档案 → 对话，全程不用命令行 |
| 👥 多好友隔离 | 每个好友一个独立数据目录（库 / 人格档案 / 媒体 / 来源存档 / 对话线），互不可见、可随时切换 |
| 🔐 密钥不落明文 | 界面里填的 API Key 存进 Windows 凭据管理器（不可用时回退当前用户 DPAPI 加密文件），永不写进 config.yaml |
| 🔒 本地优先 | 对话、分析、数据库全部在本地 SQLite；只有「生成回复」会把脱敏上下文发给你自己配置的 LLM API |
| 🧹 导入即脱敏 | 手机号/地址/证件号/银行卡等在入库时自动替换为占位符，脱敏文本是后续分析的唯一输入 |
| 🛡 隐私扫描门禁 | 内置 `check_privacy.py`：扫描代码与文档中的隐私残留，非零命中即拒绝发布（可挂 CI） |

## 下载即用（Windows）

**不想碰命令行？→ [Releases 页面](https://github.com/LZNJUPT/if_we_elsewhere/releases/latest) 下载 `IfWe-win64-v*.zip`**，
解压后双击 `IfWe/IfWe.exe`：

1. 首次启动出现引导卡，三选一：**体验示例数据**（内置虚构对话，与任何真实人物无关）、
   **导入我的记录**、**配置 LLM**；
2. 之后全部在界面里完成：导入 → 点「分析」（阶段进度实时可见、可中止）→
   看「人物档案」→ 开一条线开始对话；
3. 左侧「好友」栏可新建/切换/重命名/删除好友，每个好友的数据完全独立。

下载渠道说明：

| 渠道 | 内容 | 何时更新 |
| --- | --- | --- |
| **Releases（正式版）** | `IfWe-win64-vX.Y.Z.zip` + SHA256 | 推送 `vX.Y.Z` 标签时自动构建发布 |
| **Releases（nightly 预发布）** | `IfWe-nightly-main.zip` | 每次 `main` 分支更新，原地更新，可能含未发布改动 |
| Actions → Artifacts | 同上（保留 30 天） | 每次构建 |

几点说明：

- **数据位置**：`data/` 就在解压目录里，卸载 = 删掉整个文件夹；不写注册表、不写系统目录。
- **运行环境**：Win10 1803+ / Win11（依赖 Edge WebView2 运行时，Win11 自带；缺失时自动改用默认浏览器打开）。
- **杀软误报**：发布物为 onedir 目录、未加壳、未用 UPX；Release 页给出 `SHA256`。
  校验（PowerShell）：`Get-FileHash .\IfWe-win64-v0.3.0.zip -Algorithm SHA256`。
  若被 SmartScreen 拦截，请先核对哈希，再「更多信息 → 仍要运行」或添加信任（请不要关闭系统防护）。
- **源码运行**：下面的「3 步开始」一直可用；桌面窗口也可用
  `python run.py desktop`（需 `pip install pywebview`，不装则用默认浏览器打开）。
- 发布流程与流水线细节见 [docs/RELEASE.md](docs/RELEASE.md)。

## 3 步开始（源码运行）

```bash
# 0) 安装（Python >= 3.10）
pip install -r requirements.txt

# 1) 不导入任何真实数据，先用虚构示例体验完整界面
python run.py demo               # 或用桌面窗口: python run.py desktop

# 2) 准备好你自己的数据后：生成配置 → 导入 → 分析
python run.py init
python run.py init --source 你的记录.jsonl --sender-a "你的账号名" --sender-b "对方的账号名"
python run.py analyze            # 无 API Key 可用 python run.py analyze --skip-llm

# 3) 启动本地界面（仅监听 127.0.0.1）
python run.py server             # → http://127.0.0.1:8015
```

界面里也能把 2、3 步点完：左侧「导入记录」→ 导入向导，顶部「分析」→ 进度面板，
右侧「设置」→ 填 provider / base_url / model / key 并一键测试连接。
每个好友的数据独立存放，切换见左侧「好友」栏（命令行等价位：`run.py profile`）。

详细步骤、数据格式与常见问题见 [docs/QUICKSTART.md](docs/QUICKSTART.md) 与 [docs/IMPORT.md](docs/IMPORT.md)。

## 打包发布物（可选）

```bash
pip install pyinstaller pywebview keyring
python -m PyInstaller IfWe.spec --noconfirm --clean   # 产物：dist/IfWe/IfWe.exe（onedir）
python scripts/package_release.py                     # → dist/IfWe-win64-v<版本>.zip + .sha256
python scripts/package_release.py --verify dist/IfWe-win64-v<版本>.zip
```

打包清单写在 `IfWe.spec`（静态资源、示例数据、版本号、图标都在版本管理内）；
`package_release.py` 自带**数据红线闸门**：产物里出现 `config.yaml` / `*.db` 之类用户数据即中止。

发布（推荐走 CI，推送标签即可）：

```bash
echo "0.4.0" > VERSION          # 与 CHANGELOG.md 的版本段保持一致
git commit -am "chore(release): v0.4.0" && git push origin main
git tag -a v0.4.0 -m "IfWe v0.4.0" && git push origin v0.4.0
```

流水线会自动跑「测试 + 隐私门禁 → PyInstaller 构建 → zip/SHA256 → 离线自检 → 创建 Release」，
详见 [docs/RELEASE.md](docs/RELEASE.md)。发布后建议过一遍其中的检查清单
（含微软误报申诉与「添加信任」说明）。

## 一个诚实的边界

数字人格基于你的记录推演。无论 TA 的回应多么「像」，**TA 都不是 TA 本人**——
TA 是你记忆的回声，是模型对共同过去的即兴创作。请不要把推演内容当作
与对方沟通、做人生决策或评价对方的依据。

这个项目由一段真实的失去出发而写成。我们知道此刻读到它的人可能正处在
类似的位置：工具只能陪你到工具能到的地方，剩下的路，请交给真实世界的
朋友、家人，或专业帮助（求助资源见 [DISCLAIMER.md](DISCLAIMER.md)）。
我们也把这个边界写进了产品：界面标注「模拟估计量 · 非事实」，伦理准则
明确反对沉溺于推演而回避真实生活。

**先练习说出那句话，然后回到真实生活里去。**

## 隐私设计（差异化核心）

- **数据不出本机**：`data/` 永远被 gitignore；私有内容（聊天记录、persona）与代码彻底分离。
- **脱敏边界**：发给 LLM 的只有 `content_clean`（去噪+脱敏后的文本）与派生结论，从不发送原始记录。
- **模拟隔离**：所有对话推演写入 `sim_*` 命名空间，主库只读——推演永不污染你的真实历史。
- **好友隔离**：每个好友一个独立数据目录（`data/profiles/<id>/`），切换好友即切换整个数据上下文。
- **密钥不落明文**：界面填写的 API Key 存进 Windows 凭据管理器（服务名 `IfWe`）；不可用时回退
  当前 Windows 用户专属的 DPAPI 加密文件；`config.yaml` 与 `data/` 里都不会出现明文密钥。
- **自查工具**：`python scripts/check_privacy.py` 用红线词表扫描整个仓库，命中即非零退出。
- 详见 [PRIVACY.md](PRIVACY.md)。

## 架构一览

```
chat JSONL ──► Phase1 导入/脱敏 ──► analyze(事件/记忆/状态/转折点/persona)
                                        │
                                        ▼
              本地 Web UI ◄──► Phase15 对话服务 ◄──► DialEngine 对话内核
              (127.0.0.1)          (FastAPI)             │
                                        │               ▼
                      desktop.py / IfWe.exe      PersonaAgent(B) + 记忆检索 + 关系引擎(Phase6)
                      (pywebview 窗口)
```

数据目录布局（多好友）：

```
data/
├── profiles.json            # 好友注册表（id / 名字 / 数据目录 / 上次使用）
└── profiles/
    └── <id>/                # 每个好友一整套：库 / persona / 缓存 / 媒体 / 来源存档
        ├── ifwe_v1.db
        ├── persona/persona_v1_A.json
        ├── sources/         # 已导入来源的存档（数据规范 v2：库由它们重建而来）
        ├── sources.json     # 来源登记表（文件名 / 格式 / 逐源 A/B 映射 / 条数）
        ├── media/           # 媒体库：表情包与图片（按内容哈希去重）
        └── profile.yaml     # 可选：好友级配置覆盖全局 config.yaml
```

技术选型与取舍（为什么自研记忆而不是 Graphiti、为什么 SQLite 而不是图数据库）见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 配置速览

复制 `config.example.yaml` 为 `config.yaml` 后按需修改：双方显示名、聊天记录路径、
LLM 提供商与模型、表情包目录、模拟参数等。**密钥不要再放环境变量里也行**——
界面「设置」面板会把它写进系统凭据管理器；环境变量（`LLM_API_KEY`，可用配置改名）
仍然优先，方便 CI 或临时覆盖。任何 OpenAI 兼容端点均可（DeepSeek / GLM / 本地推理等）。

## 使用前必读

- [DISCLAIMER.md](DISCLAIMER.md) —— 非心理治疗、非预测、生成内容不代表真实他人意愿；
  如果你正处于情绪危机，请使用文档内列出的求助资源。
- [PRIVACY.md](PRIVACY.md) —— 数据流、脱敏边界、密钥存放位置、LLM 发送范围。
- **严禁**将本工具用于跟踪、骚扰或监控真实他人；你须对导入数据的合法性负责。

## 路线图

- [x] v0.2 导入格式扩展（WeChatMsg、Telegram、doctor 体检、Web 向导）
- [x] v0.3 导入链路 v2（多来源按时间合并 + 跨源去重、txt/md/docx、媒体独立通道、数据规范 v2）
- [x] v0.3 图形化全流程（界面内分析 + 进度、人物档案、LLM 设置面板、首次运行引导）
- [x] v0.3 多好友隔离（每好友一个数据目录，注册表 + 切换/删除/重命名）
- [x] v0.3 Windows 开箱即用（pywebview 桌面壳 + PyInstaller 打包清单）
- [ ] 人格档案手动编辑器（当前为只读展示；填写仍走 persona JSON）
- [ ] 研究指标脚本开放（敏感性/打分/评测——本次为控制范围未随 v0.1 发布）
- [ ] 完整版分析流水线（v0.1 为简化版：启发式/单轮 LLM 抽取）

## License

[MIT](LICENSE) + 附加条款（禁止用于跟踪/骚扰/监控真实他人）。

---

<div align="center">

「我们回不到过去，但可以练习怎么说那句话。」

</div>
