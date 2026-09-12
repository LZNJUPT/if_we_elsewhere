# -*- coding: utf-8 -*-
"""IfWe 导入体检 doctor（v0.2 O-5e）。

对用户给出的聊天记录文件做**只读**体检：格式识别、行数/消息数、时间跨度、
候选账号占比、消息类型分布、脱敏规则预估命中、可导入性结论与建议参数。
不写任何库、不修改源文件；对任何输入都不崩溃（异常归类为报告项）。

统计逻辑产品化自 poc/inspect_data.py（该文件保留不动）。
报告为 JSON 兼容 dict，供 run.py doctor 与 Web 导入预览（O-5f）共用。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import phase1_ingest as p1

TZ8 = timezone(timedelta(hours=8))

TYPE_LABELS = {
    0: "文本", 7: "图片/表情", 4: "文件", 23: "通话", 24: "小程序",
    25: "引用文本", 27: "名片", 80: "撤回", 99: "转账",
}


def _privacy_estimate(raw: list[dict]) -> dict:
    """脱敏规则预估：逐条统计各隐私模式命中次数（不改写内容）。"""
    cat_hits: Counter = Counter()
    hit_msgs = 0
    for rec in raw:
        content = rec.get("content") or ""
        n = 0
        for name, pat, _repl in p1.PRIVACY_PATTERNS:
            k = len(pat.findall(content))
            if k:
                cat_hits[name] += k
                n += k
        if n:
            hit_msgs += 1
    return {"messages_with_hits": hit_msgs,
            "by_category": dict(cat_hits.most_common())}


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ8).isoformat(timespec="seconds")


def run_doctor(source: Path) -> dict:
    """体检主入口。返回结构化报告 dict（JSON 可序列化）。"""
    from importers import registry

    report: dict = {"source_file": Path(source).name,
                    "readable": True, "recognized": False, "importable": False,
                    "importer": None, "reasons": [], "advice": [],
                    "total_rows": 0, "message_count": 0,
                    "time_span": None, "candidates": [],
                    "type_dist": {}, "privacy_estimate": None,
                    "skipped": None, "parse_error": None}

    # ---- 文件可读性 ----
    if not Path(source).is_file():
        report["readable"] = False
        report["reasons"].append(f"文件不存在或不可读: {Path(source).name}")
        return report
    try:
        size = Path(source).stat().st_size
        report["file_size_bytes"] = size
        if size == 0:
            report["reasons"].append("文件为空（0 字节）")
            return report
    except OSError as e:
        report["readable"] = False
        report["reasons"].append(f"文件状态获取失败: {e}")
        return report

    # ---- 格式识别 ----
    imp = registry.auto_detect(source)
    if imp is None:
        report["reasons"].append(
            "所有内置导入器（chatlab/WeFlow JSONL、WeChatMsg CSV、Telegram JSON）"
            "都无法识别该文件的表头/首行结构")
        report["reasons"].append(
            "若是聊天导出，请确认来源：chatlab/WeFlow 导出 .jsonl；"
            "WeChatMsg 导出 .csv；Telegram Desktop 导出 result.json")
        return report
    report["recognized"] = True
    report["importer"] = imp.source_name

    # ---- 解析（异常不外泄）----
    try:
        raw = imp.parse(source)
    except Exception as e:
        report["parse_error"] = f"{type(e).__name__}: {e}"
        report["reasons"].append(f"解析过程失败（文件可能损坏或中途截断）：{e}")
        return report
    report["message_count"] = len(raw)
    st = getattr(imp, "stats", None) or {}
    report["total_rows"] = st.get("total_rows", len(raw))
    if st:
        report["skipped"] = {k: v for k, v in st.items() if k.startswith("skipped")}
        if st.get("encoding"):
            report["source_encoding"] = st["encoding"]

    if not raw:
        report["reasons"].append(
            f"文件可识别（{imp.source_name}）但没有解析出任何消息——"
            "可能整份记录都是群聊/不支持类型，或时间戳列全部缺失")
        return report

    # ---- 统计 ----
    senders: Counter = Counter()
    types: Counter = Counter()
    ts_list: list[int] = []
    bad_ts = 0
    for rec in raw:
        senders[rec.get("accountName") or "(空账号名)"] += 1
        types[rec.get("type", -1)] += 1
        try:
            ts_list.append(int(rec["timestamp"]))
        except (KeyError, TypeError, ValueError):
            bad_ts += 1
    report["message_count_no_ts"] = bad_ts
    total = len(raw) or 1
    report["candidates"] = [{"account": a, "count": c,
                             "pct": round(c / total * 100, 2)}
                            for a, c in senders.most_common()]
    report["type_dist"] = {TYPE_LABELS.get(t, f"未知类型{t}"): n
                           for t, n in types.most_common()}
    report["privacy_estimate"] = _privacy_estimate(raw)
    if ts_list:
        report["time_span"] = {
            "first": _fmt_ts(min(ts_list)), "last": _fmt_ts(max(ts_list)),
            "days": round((max(ts_list) - min(ts_list)) / 86400, 1),
            "active_days": len({datetime.fromtimestamp(t, TZ8).date() for t in ts_list}),
        }

    # ---- 结论与建议 ----
    n_accounts = len(senders)
    top2 = senders.most_common(2)
    if n_accounts == 1:
        report["verdict"] = "可导入，但只有一方发言"
        report["importable"] = True
        report["reasons"].append(
            f"仅出现 1 个账号名（{top2[0][0]}）——双人占比门禁 G3 会失败；"
            "请确认导出的是与对方的完整对话")
    elif n_accounts == 2:
        report["verdict"] = "可导入 ✅"
        report["importable"] = True
    else:
        report["verdict"] = "可导入，但账号名超过 2 个"
        report["importable"] = True
        report["reasons"].append(
            f"出现 {n_accounts} 个不同账号名——导入时只有映射为 A/B 的两个会入库，"
            "其余消息会记为未知发送者并触发 G7 门禁")
        report["advice"].append("建议先在导出工具里筛出双人对话，或确认多账号是否为同一人的别名")

    if imp.source_name == "wecomsg" and n_accounts >= 2:
        # talker 常为 wxid，提示用体检报告里的候选名单填映射
        report["advice"].append(
            "WeChatMsg 导出的账号名通常是 wxid；请把上方候选账号中的"
            "「你本人」与「对方」分别填入 --sender-a / --sender-b")
    if imp.source_name == "wecomsg":
        report["advice"].append(
            "语音/视频/系统通知等无 canonical 对应类型的消息会被跳过"
            f"（本次：{json.dumps(report['skipped'] or {}, ensure_ascii=False)}）")

    if bad_ts:
        report["advice"].append(f"{bad_ts} 条消息缺时间戳，导入时会被跳过")

    # 建议参数
    if report["importable"] and n_accounts >= 2:
        (a_name, _), (b_name, _) = top2
        report["suggested_args"] = {
            "format": imp.source_name,
            "sender_a": a_name, "sender_b": b_name,
        }
        report["advice"].append(
            f"建议参数：--format {imp.source_name} --sender-a \"{a_name}\" "
            f"--sender-b \"{b_name}\"（A=消息较多一方仅为默认，请按实际身份调整）")
    return report


def format_report(report: dict) -> str:
    """报告 → 面向终端的可读文本。"""
    lines = [f"=== IfWe 导入体检: {report['source_file']} ==="]
    if not report["readable"]:
        lines.append(f"[X] {report['reasons'][0]}")
        return "\n".join(lines)
    if not report["recognized"]:
        lines.append("[X] 无法识别格式")
        lines.extend(f"  - {r}" for r in report["reasons"])
        return "\n".join(lines)
    lines.append(f"[i] 格式识别: {report['importer']}"
                 + (f"（编码 {report.get('source_encoding')}）"
                    if report.get("source_encoding") else ""))
    lines.append(f"[i] 总行数: {report['total_rows']} ｜ 有效消息: {report['message_count']}"
                 + (f" ｜ 缺时间戳: {report['message_count_no_ts']}"
                    if report.get("message_count_no_ts") else ""))
    ts = report.get("time_span")
    if ts:
        lines.append(f"[i] 时间跨度: {ts['first']} → {ts['last']}"
                     f"（{ts['days']} 天 / {ts['active_days']} 个有消息日）")
    if report["candidates"]:
        lines.append("[i] 候选账号（消息占比）:")
        for c in report["candidates"][:10]:
            lines.append(f"  - {c['account']}: {c['count']} 条（{c['pct']}%）")
    if report["type_dist"]:
        lines.append("[i] 消息类型分布: " +
                     "，".join(f"{k}×{v}" for k, v in report["type_dist"].items()))
    pe = report.get("privacy_estimate")
    if pe:
        cats = "，".join(f"{k}×{v}" for k, v in (pe.get("by_category") or {}).items()) or "无"
        lines.append(f"[i] 脱敏预估: {pe['messages_with_hits']} 条消息命中隐私模式（{cats}）")
    sk = report.get("skipped")
    if sk:
        lines.append(f"[i] 跳过统计: {json.dumps(sk, ensure_ascii=False)}")
    if report.get("parse_error"):
        lines.append(f"[!] 解析异常: {report['parse_error']}")
    lines.append(f"==> 结论: {report.get('verdict', '未知')}")
    for r in report["reasons"]:
        lines.append(f"  ! {r}")
    for a in report["advice"]:
        lines.append(f"  * {a}")
    return "\n".join(lines)
