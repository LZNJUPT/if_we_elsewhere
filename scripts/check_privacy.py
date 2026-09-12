# -*- coding: utf-8 -*-
"""
IfWe 隐私扫描门禁（check_privacy）

扫描发布树内全部文本文件，命中红线词表即报告（文件/行号/上下文）并**非零退出**。
可挂 pre-commit / CI：`python scripts/check_privacy.py`

红线词表内置 §2 隐私红线（昵称/事件/地点/日期锚点），支持 --extra 扩展词表
（每行一个词，# 开头为注释）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- 红线词表（可配置扩展；新增词条同时更新 PRIVACY.md） ----
REDLINE_WORDS: list[str] = [
    # 真实昵称（及可关联称呼）
    "隔壁王姐姐", "Heimlich", "但为君故",
    # 真实事件情节
    "取保候审", "摊牌", "复合被拒", "情人节送玫瑰",
    # 真实地点组合
    "徐州", "南京", "南邮", "玄武湖", "仙林", "云锦路",
    # 真实日期锚点（作为决策点/事件出现时）
    "2025-06-26", "2025-09-16", "2026-04-04", "2026-07-26",
    # 私有环境痕迹
    "weflow_bendi",
]

# 扫描的文本扩展名
TEXT_EXTS = {".py", ".md", ".js", ".css", ".html", ".sql", ".yaml", ".yml",
             ".json", ".jsonl", ".txt", ".toml", ".cfg", ".ini"}

# 跳过的目录
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
             "data", "data_demo", ".cache_fastembed", "build", "dist"}


def load_extra(path: Path | None) -> list[str]:
    if path is None:
        return []
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        w = line.strip()
        if w and not w.startswith("#"):
            words.append(w)
    return words


def iter_text_files(root: Path):
    self_path = Path(__file__).resolve()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.resolve() == self_path:      # 词表文件自身自排除
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        yield p


def scan(root: Path, words: list[str]) -> list[dict]:
    hits: list[dict] = []
    for p in iter_text_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for w in words:
                idx = line.find(w)
                if idx >= 0:
                    s = max(0, idx - 30)
                    ctx = line[s:idx + len(w) + 30].strip()
                    hits.append({"file": str(p.relative_to(root)), "line": lineno,
                                 "word": w, "context": ctx})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="IfWe 隐私扫描门禁")
    ap.add_argument("--extra", type=str, default="", help="扩展词表文件（每行一个词）")
    ap.add_argument("--root", type=str, default=str(ROOT), help="扫描根目录（默认发布树根）")
    args = ap.parse_args()

    words = REDLINE_WORDS + load_extra(Path(args.extra) if args.extra else None)
    hits = scan(Path(args.root), words)

    if hits:
        print(f"[check_privacy] 命中 {len(hits)} 处，禁止发布：\n")
        for h in hits:
            print(f"  {h['file']}:{h['line']}  词「{h['word']}」")
            print(f"    …{h['context']}…")
        print("\n处理方式：改写为通用文案，或确认属于误报后从词表/该文件中移除。")
        return 1
    print(f"[check_privacy] 0 命中 ✅（词表 {len(words)} 条，范围 {args.root}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
