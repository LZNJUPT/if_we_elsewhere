#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IfWe · 生成 GitHub Release 说明（从 CHANGELOG 抽版本段落 + 下载/校验/使用指引）

用法：
    python scripts/release_notes.py --version v0.3.0 --zip dist/IfWe-win64-v0.3.0.zip
    python scripts/release_notes.py --version v0.3.0 --check      # 只做一致性校验（CI 用）

--check 校验三件事，任一不符即非零退出：
    1. 传入版本（tag）与仓库根 VERSION 一致；
    2. CHANGELOG.md 里有该版本的段落；
    3. 版本号形如 vX.Y.Z（或 X.Y.Z）。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

# 控制台非 UTF-8 时打印中文会崩（Windows GBK / CI runner cp1252）
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def read_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def changelog_section(version: str) -> str:
    """取 CHANGELOG.md 里 `## vX.Y.Z` 到下一个 `## ` 之间的正文（不含标题行）"""
    try:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = bool(re.match(rf"^##\s+v?{re.escape(version)}\b", line))
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="IfWe Release 说明生成")
    ap.add_argument("--version", required=True, help="版本或 tag（v0.3.0 / 0.3.0）")
    ap.add_argument("--zip", default="", help="发布物 zip（用于算 SHA256）")
    ap.add_argument("--out", default="", help="输出文件（默认打印到 stdout）")
    ap.add_argument("--prefix", default="", help="追加在开头的提示段落（nightly 构建用）")
    ap.add_argument("--check", action="store_true", help="只做一致性校验，不生成说明")
    args = ap.parse_args()

    version = args.version.strip().lstrip("v")
    file_version = read_version()
    section = changelog_section(version)

    problems: list[str] = []
    if not SEMVER.match(args.version.strip()):
        problems.append(f"版本号格式不合法：{args.version}（应为 vX.Y.Z）")
    if file_version and version != file_version:
        problems.append(f"tag 版本 {version} 与仓库 VERSION（{file_version}）不一致")
    if not section:
        problems.append(f"CHANGELOG.md 里没有 v{version} 的段落")

    if args.check:
        if problems:
            print("[release-notes] 校验未通过：")
            for p in problems:
                print("   -", p)
            return 1
        print(f"[release-notes] 版本校验通过：v{version}（VERSION / CHANGELOG 一致）")
        return 0

    if problems:
        print("[release-notes] 警告：")
        for p in problems:
            print("   -", p)

    zip_path = Path(args.zip) if args.zip else None
    zip_name = zip_path.name if zip_path else f"IfWe-win64-v{version}.zip"
    digest = sha256_of(zip_path) if (zip_path and zip_path.is_file()) else "（构建过程中计算）"
    size_mb = f"{zip_path.stat().st_size / 1024 / 1024:.1f} MB" if (zip_path and zip_path.is_file()) else "—"

    notes = f"""**Windows 免安装版（不需要 Python、不需要装依赖）**

| 文件 | 大小 | SHA256 |
|---|---|---|
| `{zip_name}` | {size_mb} | `{digest}` |

### 三步开始

1. 下载上面的 zip，解压到任意目录（例如 `D:\\IfWe`）；
2. 双击 `IfWe/IfWe.exe`（首次启动会出现引导卡：体验示例数据 / 导入我的记录 / 配置 LLM）；
3. 之后全部在界面里完成：导入 → **分析**（带阶段进度，可中止）→ 看**人物档案** → 开一条线开始对话。

左侧「好友」栏可新建 / 切换 / 重命名 / 删除好友，每个好友的数据完全独立。

### 你需要知道

- **数据只在你自己电脑上**：库、人格档案、对话全部写在解压目录的 `data/` 里；卸载 = 删目录。
  只有「生成回复」会把**脱敏后**的上下文发给你自己配置的 LLM API。
- **密钥不落明文**：界面「设置」里填的 Key 存进 Windows 凭据管理器（回退为当前用户的 DPAPI 加密文件），
  `config.yaml` 里永远没有明文密钥。
- **运行环境**：Windows 10 1803+ / Windows 11（需 Edge WebView2 运行时，Win11 自带；缺失时自动用默认浏览器打开）。
- **完整性校验**（PowerShell）：
  `Get-FileHash .\\{zip_name} -Algorithm SHA256` —— 与上表 SHA256 一致即未被篡改。
- **杀软/SmartScreen 提示**：本包为 onedir、未加壳、未用 UPX。核对哈希后选「更多信息 → 仍要运行」
  或把解压目录加入信任；请不要直接关闭系统防护。
- 详细说明见包内 `使用说明.txt`、`README.md`、`PRIVACY.md`、`DISCLAIMER.md`。

### 本版改动

{section or "（CHANGELOG 暂无该版本段落）"}

---

> 数字人格是对你记忆的回声，**不是 TA 本人**；请勿用于跟踪、骚扰或监控真实他人。
"""
    if args.prefix:
        notes = notes.replace("### 三步开始",
                              f"{args.prefix.strip()}\n\n### 三步开始", 1)
    if args.out:
        Path(args.out).write_text(notes, encoding="utf-8")
        print(f"[release-notes] 已写入 {args.out}")
    else:
        print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
