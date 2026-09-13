#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IfWe · 发布物打包（把 PyInstaller 产物打成可分发的 zip + SHA256）

本地用法（先 `pyinstaller IfWe.spec --noconfirm` 生成 dist/IfWe/）：
    python scripts/package_release.py
    python scripts/package_release.py --dist dist/IfWe --outdir dist --version 0.3.0

产物：
    dist/IfWe-win64-v<版本>.zip          解压后双击 IfWe/IfWe.exe 即用
    dist/IfWe-win64-v<版本>.zip.sha256   校验值（sha256sum 格式）

安全：打包前扫描产物目录，发现任何 config.yaml / *.db 等用户数据立即中止
（发布物里永远不该出现 data/，这是隐私红线，也是 CI 的一道闸）。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 控制台不是 UTF-8（Windows GBK / runner cp1252）时，打印中文会 UnicodeEncodeError。
# 统一把标准流改成 UTF-8 + 宽容替换，保证脚本在任何终端都能跑完。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

# 随机附带在包根的说明文件（都是在版本管理内的公开文档）
EXTRA_FILES = ["README.md", "DISCLAIMER.md", "PRIVACY.md", "LICENSE", "VERSION",
               "config.example.yaml", "docs/QUICKSTART.md"]
# 绝不允许出现在发布物里的文件（用户数据红线）
FORBIDDEN_NAMES = {"config.yaml"}
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")


def read_version(explicit: str = "") -> str:
    if explicit:
        return explicit.lstrip("v")
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def scan_forbidden(bundle: Path) -> list[str]:
    """返回发布物里不该存在的数据文件（空 = 干净）"""
    bad: list[str] = []
    for p in bundle.rglob("*"):
        if not p.is_file():
            continue
        if p.name in FORBIDDEN_NAMES or p.suffix.lower() in FORBIDDEN_SUFFIXES:
            bad.append(str(p.relative_to(bundle)))
    return bad


REQUIRED_SUFFIXES = (
    "IfWe.exe",                       # 主程序（Windows 构建的判据）
    "app/phase15_web/index.html",     # 界面静态资源
    "sample_data/chat.sample.jsonl",  # 示例数据（引导卡「体验示例数据」要用）
    "VERSION",                        # 版本号（/api/health 回显）
    "使用说明.txt",                    # 面向用户的第一步指引
)


def verify_zip(path: Path) -> int:
    """校验发布 zip 的结构完整性 + 无用户数据（CI 与本地发布前都能跑）"""
    if not path.is_file():
        print(f"[verify] 找不到 {path}")
        return 2
    names = zipfile.ZipFile(path).namelist()
    problems: list[str] = []
    for suf in REQUIRED_SUFFIXES:
        if not any(n == suf or n.endswith("/" + suf) for n in names):
            problems.append(f"缺少必需条目：{suf}")
    bad = [n for n in names
           if Path(n).name in FORBIDDEN_NAMES or Path(n).suffix.lower() in FORBIDDEN_SUFFIXES]
    if bad:
        problems.append("疑似用户数据：" + ", ".join(bad[:5]))
    if not any("/_internal/" in n or n.endswith("_internal") for n in names):
        problems.append("缺少 PyInstaller 运行时目录（_internal/），产物可能不完整")
    print(f"[verify] {path.name}：{len(names)} 个条目，{path.stat().st_size / 1024 / 1024:.1f} MB，"
          f"SHA256 {sha256_of(path)[:16]}…")
    if problems:
        print("[verify] 未通过：")
        for p in problems:
            print("   -", p)
        return 1
    print("[verify] 结构完整、无用户数据 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="IfWe 发布物打包")
    ap.add_argument("--dist", default="dist/IfWe", help="PyInstaller 产物目录（默认 dist/IfWe）")
    ap.add_argument("--outdir", default="dist", help="zip 输出目录（默认 dist）")
    ap.add_argument("--version", default="", help="版本号（默认读仓库根 VERSION）")
    ap.add_argument("--name", default="", help="zip 主名（默认 IfWe-win64-v<版本>）")
    ap.add_argument("--verify", default="", help="只校验既有 zip（CI 用）")
    args = ap.parse_args()

    if args.verify:
        p = Path(args.verify)
        if not p.is_absolute():
            p = ROOT / p
        return verify_zip(p)

    bundle = (ROOT / args.dist).resolve() if not Path(args.dist).is_absolute() else Path(args.dist)
    outdir = (ROOT / args.outdir).resolve() if not Path(args.outdir).is_absolute() else Path(args.outdir)
    version = read_version(args.version)
    name = args.name or f"IfWe-win64-v{version}"

    if not bundle.is_dir():
        print(f"[package] 找不到产物目录 {bundle}；先执行: pyinstaller IfWe.spec --noconfirm")
        return 2
    exe = next((e for e in ("IfWe.exe", "IfWe") if (bundle / e).is_file()), "")
    if not exe:
        print(f"[package] {bundle} 里没有 IfWe.exe / IfWe，产物不完整")
        return 2

    bad = scan_forbidden(bundle)
    if bad:
        print("[package] 中止：发布物里检测到疑似用户数据文件（隐私红线）：")
        for b in bad[:10]:
            print("   -", b)
        print("   请确认 data/ 未被带进产物，再重新打包。")
        return 3

    outdir.mkdir(parents=True, exist_ok=True)
    zip_path = outdir / f"{name}.zip"
    if zip_path.exists():
        zip_path.unlink()

    # 为了包内保持 `IfWe/...` 结构，用临时目录拼装一份「发布根」
    with tempfile.TemporaryDirectory(prefix="ifwe_pkg_") as tmp:
        stage = Path(tmp) / "release"
        stage.mkdir(parents=True)
        shutil.copytree(bundle, stage / bundle.name)
        for rel in EXTRA_FILES:
            src = ROOT / rel
            if not src.is_file():
                continue
            dst = stage / Path(rel).name
            shutil.copy2(src, dst)
        (stage / "使用说明.txt").write_text(_readme_txt(version, bundle.name), encoding="utf-8")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(stage).as_posix())

    digest = sha256_of(zip_path)
    (zip_path.with_suffix(".zip.sha256")).write_text(f"{digest}  {zip_path.name}\n",
                                                     encoding="utf-8")
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"[package] 完成：{zip_path}")
    print(f"[package] 大小 {size_mb:.1f} MB ｜ 内含 {sum(1 for _ in zipfile.ZipFile(zip_path).namelist())} 个条目")
    print(f"[package] SHA256 {digest}")
    return 0


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _readme_txt(version: str, folder: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""IfWe · 若我们在  v{version}（构建于 {stamp} UTC）
================================================

怎么用（不需要 Python，不需要装任何东西）
------------------------------------------
1. 把本压缩包解压到任意目录（例如 D:\\IfWe）；
2. 双击 {folder}\\IfWe.exe；
3. 首次启动会出现引导卡，三选一：
     · 体验示例数据  —— 内置虚构对话，先看完整界面（不涉及任何真实数据）
     · 导入我的记录  —— 支持 chatlab / WeFlow / WeChatMsg / Telegram 导出的文件
     · 配置 LLM      —— 填 Base URL / 模型 / 密钥，解锁对话推演

界面里能做完所有事：导入 → 分析（带进度、可中止）→ 人物档案 → 开一条线开始对话。
左侧「好友」栏可以建多个好友，每个好友的数据完全独立。

数据在哪 / 怎么卸载
-------------------
· 所有数据（库、人格档案、对话）都在解压目录下的 data\\ 里，不上传任何远端；
· 卸载 = 删掉整个解压目录；程序不写注册表、不写系统目录。

密钥怎么存
----------
界面「设置」里填的 API Key 会存进 Windows 凭据管理器（服务名 IfWe）；
不可用时回退为「当前 Windows 用户」的 DPAPI 加密文件。config.yaml 里永远没有明文密钥。

运行环境
--------
Windows 10 1803+ / Windows 11（需要 Edge WebView2 运行时，Win11 自带；
缺失时会自动改用默认浏览器打开）。

安全校验 / 杀软提示
-------------------
· 本包为免安装 onedir 构建，未加壳、未使用 UPX；
· 校验压缩包完整性（PowerShell）：
    Get-FileHash .\\{folder}.zip -Algorithm SHA256
  与 Release 页给出的 SHA256 对比，一致即未被篡改；
· 若被 SmartScreen / 杀软拦截：核对哈希后选「更多信息 → 仍要运行」，
  或把解压目录加入信任；请不要直接关闭系统防护。

边界与免责
----------
数字人格是对你记忆的回声，不是对方本人。请勿用于跟踪、骚扰或监控真实他人；
使用前请阅读 PRIVACY.md 与 DISCLAIMER.md（同目录）。若你正处在情绪危机中，
请寻求真实世界的帮助（见 DISCLAIMER.md 内的求助资源）。
"""


if __name__ == "__main__":
    sys.exit(main())
