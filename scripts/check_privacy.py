# -*- coding: utf-8 -*-
"""
IfWe 隐私扫描门禁（check_privacy）

扫描发布树内全部文本文件，命中红线词表即报告（文件/行号/上下文）并**非零退出**。
可挂 pre-commit / CI：`python scripts/check_privacy.py`

红线词表内置 §2 隐私红线（昵称/事件/地点/日期锚点），支持 --extra 扩展词表
（每行一个词，# 开头为注释）。

v2（审计加固）：匹配前对词条与行文本做**形态归一化**——斜杠方向（反斜杠与正斜杠互认）、
空格折叠、ASCII 大小写——堵住「D:/weflow」「44期」这类绕过精确匹配的变体
逃逸（2026-09-12 隐私审计发现）。含真实姓名/统计指纹的扩展词不走本文件，
放在仓库外本地词表，经 --extra 传入（见 PRIVACY.md「隐私扫描门禁」）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 控制台不是 UTF-8（Windows GBK）时打印 ✅ 会 UnicodeEncodeError（2026-09-12 实测），
# 统一把标准流改成 UTF-8 + 宽容替换。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

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
             "data", "data_demo", ".cache_fastembed", "build", "dist",
             ".workbuddy"}   # v2: AI 工作台平台目录（本地状态，已 gitignore，非项目内容）
# 跳过的目录名前缀（多好友迁移备份等：目录里是本机真实数据，不参与发布扫描）
SKIP_PREFIXES = ("data_backup_",)


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
        parts = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if any(part.startswith(SKIP_PREFIXES) for part in parts[:-1]):
            continue
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        yield p


def _word_variants(word: str) -> list[str]:
    """词条形态变体：原始 / 斜杠归一 / 去空格 / 双归一（v2 防变体逃逸）。"""
    forms = {
        word,
        word.replace("\\", "/"),
        "".join(word.split()),
        "".join(word.split()).replace("\\", "/"),
    }
    return sorted(f for f in forms if f)


def _line_forms(line: str) -> tuple[str, ...]:
    """行文本形态：原始 / 斜杠归一 / 去空格 / 双归一（与 _word_variants 配对使用）。"""
    collapsed = "".join(line.split())
    return (line, line.replace("\\", "/"), collapsed,
            collapsed.replace("\\", "/"))


def scan(root: Path, words: list[str]) -> list[dict]:
    hits: list[dict] = []
    # 变体 -> 原词（报告仍用原词）；大小写不敏感（ASCII 场景经 casefold 生效）
    variant_map: dict[str, str] = {}
    for w in words:
        for v in _word_variants(w):
            variant_map.setdefault(v.casefold(), w)
    for p in iter_text_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            seen_words: set[str] = set()   # 每行每词只报一次（多变体不重复计数）
            for form in _line_forms(line):
                folded = form.casefold()
                for v, w in variant_map.items():
                    if w in seen_words:
                        continue
                    idx = folded.find(v)
                    if idx >= 0:
                        seen_words.add(w)
                        s = max(0, idx - 30)
                        ctx = line[s:idx + len(v) + 30].strip()
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
