# -*- coding: utf-8 -*-
"""
import_chat · 把你自己导出的聊天记录（chatlab/WeFlow JSONL）导入 IfWe 规范库

用法（项目根执行）:
  python scripts/import_chat.py --source path/to/chat.jsonl \
      --sender-a "你的账号名" --sender-b "对方的账号名"

说明：
- 本项目**不解析微信数据库、不提供任何抓取功能**，仅处理你已合法导出的文件
- 导入即脱敏（手机号/地址/证件号等 → 占位符），content_clean 是后续分析与 LLM 的唯一输入
- 默认全量重建（--no-reset 复用已有库）
- 也可把账号名映射写进 config.yaml（people.A.match / people.B.match）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import config as cfg_mod                      # noqa: E402
from importers import registry                 # noqa: E402
from phase1_ingest import ingest              # noqa: E402


def sender_map_from(args) -> dict[str, str]:
    people = cfg_mod.load()["people"]
    m = {}
    m[(args.sender_a or people.get("A", {}).get("match") or "").strip()] = "A"
    m[(args.sender_b or people.get("B", {}).get("match") or "").strip()] = "B"
    m.pop("", None)
    if len(m) < 2:
        raise SystemExit("需要 A/B 两个账号名映射：用 --sender-a/--sender-b，"
                         "或写进 config.yaml 的 people.A.match / people.B.match")
    return m


def pick_importer(args, src: Path):
    """v0.2 O-5a: --format auto（默认）时按注册表自动探测，未命中回退 chatlab
    并参考 config chat.format；显式格式强制指定 importer。"""
    if args.format != "auto":
        imp = registry.by_name(args.format)
        if imp is None:
            raise SystemExit(f"未知导入格式: {args.format}（可选: {', '.join(registry.names())}）")
        return imp
    imp = registry.auto_detect(src)
    if imp is not None:
        print(f"[i] 自动识别源格式: {imp.source_name}")
        return imp
    fmt = cfg_mod.load()["chat"]["format"]
    imp = registry.by_name(fmt)
    if imp is None:
        from importers.chatlab_jsonl import ChatlabJsonlImporter
        imp = ChatlabJsonlImporter()
    print(f"[warn] 未自动识别源格式，按 chat.format={imp.source_name} 回退解析")
    return imp


def main() -> int:
    ap = argparse.ArgumentParser(description="导入你自己导出的聊天记录 → IfWe 规范库")
    ap.add_argument("--source", type=str, default="",
                    help="导出的聊天记录文件路径（默认取 config chat.source）")
    ap.add_argument("--format", type=str, default="auto",
                    choices=["auto"] + registry.names(),
                    help="源格式：auto=自动探测（默认）；chatlab=WeFlow JSONL；"
                         "wecomsg=WeChatMsg(MemoTrace) 导出；telegram=Telegram Desktop 导出 JSON")
    ap.add_argument("--sender-a", type=str, default="", help="你本人的原始账号名 → A")
    ap.add_argument("--sender-b", type=str, default="", help="对方的原始账号名 → B")
    ap.add_argument("--no-reset", action="store_true", help="复用已有库（默认全量重建）")
    args = ap.parse_args()

    cfg = cfg_mod.load()
    if cfg["chat"]["timezone"] != "Asia/Shanghai":
        print("[warn] v1 导入固定按东八区(Asia/Shanghai)解析时间戳；其他时区字段预留未生效")
    if cfg["privacy"]["sanitize"] != "medium":
        print("[warn] v1 仅提供 medium 脱敏档位；其余档位字段预留未生效")

    src = Path(args.source or cfg["chat"]["source"])
    if not src.is_absolute():
        src = ROOT / src
    if not src.is_file():
        raise SystemExit(f"源文件不存在: {src}")

    smap = sender_map_from(args)
    print(f"[i] 源文件: {src}")
    print(f"[i] 账号名映射: {smap}（库内只存 A/B 代号，sender_orig 仅本地保留）")
    imp = pick_importer(args, src)
    summary = ingest(src, smap, db_path=cfg_mod.db_path(),
                     session_gap_s=int(cfg["chat"]["session_gap_minutes"]) * 60,
                     no_reset=args.no_reset, importer=imp)
    if summary.get("gates_all_pass"):
        try:
            stored = src.relative_to(ROOT).as_posix()
        except ValueError:
            stored = src.as_posix()
        if cfg_mod.persist_import_config(stored, smap):
            print("[i] 已将 source/账号映射写入 config.yaml（下次导入无需再带映射参数）")
    print("\n导入完成。下一步: python run.py analyze（生成事件/记忆/关系状态/人格）")
    return 0 if summary.get("gates_all_pass") else 1


if __name__ == "__main__":
    sys.exit(main())
