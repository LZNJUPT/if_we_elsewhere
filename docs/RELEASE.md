# RELEASE · 发布流水线（CI 自动出成品）

目标：**用户不用下载源代码**——每次更新都会产出一个可直接运行的 Windows 免安装包，
挂在 GitHub Releases 上（外加 Actions Artifacts 里 30 天内的构建产物）。

## 两条发布通道

| 触发 | 产物 | 挂在哪 | 说明 |
| --- | --- | --- | --- |
| 推送 `vX.Y.Z` 标签 | `IfWe-win64-vX.Y.Z.zip` + `.sha256` | **正式 Release**（标记 Latest） | 给用户的稳定版；标签必须与 `VERSION`、`CHANGELOG.md` 一致，否则流水线直接失败 |
| 推送到 `main` | `IfWe-nightly-main.zip` + `.sha256` | `nightly` 预发布（原地更新） | 永远能下到「最新 main 构建」；不占用 Latest |
| Pull Request | 无（只跑校验） | — | 测试 + 隐私门禁，省 CI 时长 |
| 手动 `workflow_dispatch` | 与 main 相同 | `nightly` | 需要立刻重跑时用 |

流水线文件：`.github/workflows/release.yml`（校验 `verify` → 构建 `build-windows` → 发布）。

## 切一个正式版本（三步）

```bash
# 1) 改版本号 + 写 changelog（两处必须一致，CI 会校验）
echo "0.4.0" > VERSION
#    CHANGELOG.md 顶部加一段：## v0.4.0 (2026-XX-XX) + 内容

# 2) 提交
git add -A && git commit -m "chore(release): v0.4.0"

# 3) 打标签并推送（推送标签即触发发布）
git tag -a v0.4.0 -m "IfWe v0.4.0"
git push origin main
git push origin v0.4.0
```

> 本机 bash 没有 coreutils、且 HTTPS 到 github.com 被代理拦截，push 请走 SSH：
> `git -c core.sshCommand="C:/Users/李治/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin/ssh.exe" push origin v0.4.0`
> 详细环境说明见 `windows-git-remote-ops` 技能。

约 5~10 分钟后，Releases 页出现 `IfWe v0.4.0`，含 zip 与 SHA256；Release 说明由
`scripts/release_notes.py` 从 CHANGELOG 自动生成（下载指引 + 校验 + 三步开始 + 边界声明）。

## 本地自己出一份包（不上 CI 也能发）

```bash
pip install -r requirements.txt pyinstaller pywebview
python -m PyInstaller IfWe.spec --noconfirm --clean     # → dist/IfWe/
python scripts/package_release.py                       # → dist/IfWe-win64-v<VERSION>.zip + .sha256
python scripts/package_release.py --verify dist/IfWe-win64-v0.3.0.zip   # 结构 + 无用户数据
```

打包脚本内置**数据红线闸门**：产物里若出现 `config.yaml` / `*.db` 等用户数据，
直接中止并列出文件名（发布物里永远不该有 `data/`）。

## 流水线里都做了什么校验

1. `verify`（ubuntu）：`python -m unittest discover -s tests` + `scripts/check_privacy.py`
   （红线词表 0 命中）+ 发布脚本自检；打标签时额外校验 `tag == VERSION == CHANGELOG`。
2. `build-windows`：
   - PyInstaller onedir 构建（`IfWe.spec`，不加壳、不用 UPX）；
   - 打包 zip + SHA256（含数据泄漏闸门）；
   - `--verify` 检查 zip 结构（`IfWe.exe`、界面静态资源、示例数据、`VERSION`、使用说明、`_internal/`）；
   - **成品冒烟**：真的把打包好的 `IfWe.exe --no-window --port 8231` 启动一次，轮询
     `/api/health`（校验版本号已打进包）、`/api/profiles`、`/api/settings`、`/api/persona`、
     `/api/onboarding`、首页，然后关闭——确保「下载下来能跑」不是靠猜；
   - **离线自检**：在隔离的临时数据目录跑 `app/phase15_dial_engine.py selftest`
     （世界构建 → 回合 → 落库 → 主库隔离 → 跨天 → 重启续聊），不需要 API Key，也绝不碰真实数据；
   - 上传 Actions Artifact（无需登录即可下载）。
3. `publish`：用 `gh` 创建或更新 Release，zip 与 SHA256 一起挂上；说明里附 commit 与工作流链接。

> `desktop.py --no-window` 是给上面这种自动化场景准备的：只起本地服务、不开窗口也不开浏览器，
> 并在日志里输出 `[desktop] READY http://127.0.0.1:<port>`，方便脚本判定就绪。

## 发布后自检清单

- [ ] Releases 页能看到最新版本，`Latest` 标记正确（nightly 不应被标成 Latest）；
- [ ] zip 能下载，大小合理（约 60~120 MB），`SHA256` 与 `.sha256` 文件一致；
- [ ] 在干净 Windows 上解压 → 双击 `IfWe.exe` → 引导卡出现 → 示例数据可跑通；
- [ ] 关闭窗口后进程树干净（无孤儿 python/uvicorn）；
- [ ] 发布树里没有 `data/`、`config.yaml`、`*.db`；
- [ ] 若被 SmartScreen/杀软拦截：核对哈希 → 走微软误报申诉 → 发布说明给「添加信任」步骤
      （不要建议用户关闭防护）。

## 常见问题

**Q: 只想重新出一份包，不想发版？**
推到 `main` 就会更新 `nightly`；或 Actions 页手动 `Run workflow`。

**Q: 标签推了但流水线失败？**
最常见原因是 `VERSION`/`CHANGELOG` 与标签不一致——`verify` 阶段会明确打印哪一个对不上。

**Q: 能不能出 macOS/Linux 包？**
当前只做 Windows（P3 的目标就是「双击即用」）。要跨平台需给 `IfWe.spec` 做平台分支，
并解决 pywebview 在 Linux 需要 GTK 的问题（未列入本期）。
