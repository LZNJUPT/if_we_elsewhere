# -*- coding: utf-8 -*-
"""
import_chat · 把你自己合法导出的聊天记录导入 IfWe 规范库（多来源可一次导入）

用法（项目根执行）:

  # 单份（老用法不变）
  python scripts/import_chat.py --source chat.jsonl \
      --sender-a "你的账号名" --sender-b "对方的账号名"

  # 多份来源一次导入：不同应用各自导出的记录，按时间自动合并、重复消息自动去重
  python scripts/import_chat.py \
      --source wechat.csv  --sender-a wxid_me   --sender-b wxid_ta \
      --source tg.json     --sender-a user_me   --sender-b user_ta

说明：
- 本项目**不解析微信数据库、不提供任何抓取功能**，仅处理你已合法导出的文件
- 导入即脱敏（手机号/地址/证件号等 → 占位符），content_clean 是后续分析与 LLM 的唯一输入
- 每次导入默认把来源**登记存档**（`data/.../sources/` + `sources.json`），
  并从「全部已登记来源」重建一次库 —— 所以分多次导入不会互相覆盖
- `--sender-a/--sender-b` 可重复给出：按顺序与每一份来源的候选账号匹配，
  匹配不上会报出该来源的候选名单
- `--no-reset`：不重建，把给定来源追加写入现有库（旧行为，不做来源登记）
- 也可把账号名映射写进 config.yaml 的 people.A.match / people.B.match（支持列表）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import config as cfg_mod                      # noqa: E402
import import_sources                         # noqa: E402
from doctor import run_doctor                 # noqa: E402
from importers import registry                # noqa: E402
from phase1_ingest import ingest_many         # noqa: E402


def _names(v) -> list[str]:
    """账号名参数/配置项 → 名字列表（str 或 list 都接受）"""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()] if v else []


def _pick_importer(fmt: str, src: Path):
    if fmt != "auto":
        imp = registry.by_name(fmt)
        if imp is None:
            raise SystemExit(f"未知导入格式: {fmt}（可选: {', '.join(registry.names())}）")
        return imp
    imp = registry.auto_detect(src)
    if imp is not None:
        print(f"[i] 自动识别源格式: {imp.source_name}")
        return imp
    fallback = cfg_mod.load()["chat"]["format"]
    imp = registry.by_name(fallback)
    if imp is None:
        from importers.chatlab_jsonl import ChatlabJsonlImporter
        imp = ChatlabJsonlImporter()
    print(f"[warn] 未自动识别源格式，按 chat.format={imp.source_name} 回退解析")
    return imp


def resolve_by_source(paths: list[Path], args):
    """逐源体检并解析 A/B 映射（不同应用的账号名不同，所以必须逐源解析）。"""
    people = cfg_mod.load()["people"]
    a_names = _names(args.sender_a) + _names(people.get("A", {}).get("match"))
    b_names = _names(args.sender_b) + _names(people.get("B", {}).get("match"))
    specs, meta = [], []
    for src in paths:
        report = run_doctor(src)
        if not report.get("recognized") or not report.get("message_count"):
            raise SystemExit(
                f"「{src.name}」解析不出消息（{report.get('verdict') or '格式未识别'}）。\n"
                + "\n".join(f"  - {r}" for r in report.get("reasons") or []))
        cands = [c["account"] for c in report.get("candidates") or []]
        a = next((n for n in a_names if n in cands), "")
        b = next((n for n in b_names if n in cands), "")
        if not a or not b or a == b:
            raise SystemExit(
                f"「{src.name}」的 A/B 映射无法确定。\n"
                f"  该来源出现的账号: {', '.join(cands) or '（无）'}\n"
                f"  已给的 --sender-a: {a_names or '（无）'}；--sender-b: {b_names or '（无）'}\n"
                f"  请为这份来源补一个 --sender-a/--sender-b（可重复给出以覆盖多来源）")
        specs.append({"path": src, "sender_map": {a: "A", b: "B"},
                      "name": src.name, "importer": _pick_importer(args.format, src)})
        meta.append({"path": src, "name": src.name, "importer": report.get("importer") or "",
                     "sender_map": {a: "A", b: "B"},
                     "message_count": report.get("message_count") or 0,
                     "time_span": report.get("time_span")})
        print(f"[i] {src.name}: {report['importer']} ／ {report['message_count']} 条 ／ "
              f"映射 {a}→A, {b}→B")
    return specs, meta


def main() -> int:
    ap = argparse.ArgumentParser(description="导入你自己导出的聊天记录 → IfWe 规范库（支持多来源）")
    ap.add_argument("--source", type=str, action="append", default=[],
                    help="导出的聊天记录文件路径，可重复给出多份（默认取 config chat.source）")
    ap.add_argument("--format", type=str, default="auto",
                    choices=["auto"] + registry.names(),
                    help="源格式：auto=自动探测（默认）")
    ap.add_argument("--sender-a", type=str, action="append", default=[],
                    help="你本人的原始账号名（可重复，按来源分别匹配）")
    ap.add_argument("--sender-b", type=str, action="append", default=[],
                    help="对方的原始账号名（可重复，按来源分别匹配）")
    ap.add_argument("--no-reset", action="store_true",
                    help="不重建：把给定来源追加写入现有库（旧行为，不做来源登记）")
    ap.add_argument("--no-archive", action="store_true",
                    help="不把来源登记进 sources.json（一次性导入，之后无法单独移除）")
    args = ap.parse_args()

    cfg = cfg_mod.load()
    if cfg["chat"]["timezone"] != "Asia/Shanghai":
        print("[warn] v1 导入固定按东八区(Asia/Shanghai)解析时间戳；其他时区字段预留未生效")
    if cfg["privacy"]["sanitize"] != "medium":
        print("[warn] v1 仅提供 medium 脱敏档位；其余档位字段预留未生效")

    raw_paths = args.source or [cfg["chat"]["source"]]
    paths: list[Path] = []
    for s in raw_paths:
        p = Path(s)
        if not p.is_absolute():
            p = ROOT / p
        if not p.is_file():
            raise SystemExit(f"源文件不存在: {p}")
        paths.append(p)

    specs, meta = resolve_by_source(paths, args)
    gap_s = int(cfg["chat"]["session_gap_minutes"]) * 60

    if args.no_reset:
        summary = ingest_many(specs, db_path=cfg_mod.db_path(),
                              session_gap_s=gap_s, no_reset=True)
    else:
        if not args.no_archive:                  # 先归档登记，再从全部来源重建
            for m in meta:
                first = (m["time_span"] or {}).get("first")
                last = (m["time_span"] or {}).get("last")
                import_sources.add_source(
                    m["path"], m["name"], m["importer"], m["sender_map"],
                    message_count=m["message_count"],
                    first_ts=_ts(first), last_ts=_ts(last))
        all_specs, missing = import_sources.build_source_specs()
        if missing:
            print(f"[warn] 这些已登记来源的存档缺失，本次未参与: {', '.join(missing)}")
        use = all_specs or specs
        if len(use) > len(specs):
            print(f"[i] 从 {len(use)} 份已登记来源重建（含历史导入）")
        summary = ingest_many(use, db_path=cfg_mod.db_path(), session_gap_s=gap_s)

    if summary.get("gates_all_pass"):
        try:
            stored = paths[0].relative_to(ROOT).as_posix()
        except ValueError:
            stored = paths[0].as_posix()
        if len(paths) == 1 and cfg_mod.persist_import_config(stored, specs[0]["sender_map"]):
            print("[i] 已将 source/账号映射写入 config.yaml（下次导入无需再带映射参数）")
    print(f"\n导入完成（来源 {summary.get('source_count')} 份，"
          f"去重移除 {summary.get('duplicates_removed')} 条）。"
          f"下一步: python run.py analyze")
    return 0 if summary.get("gates_all_pass") else 1


def _ts(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(iso_str).timestamp())
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
