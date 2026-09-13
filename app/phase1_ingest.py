# -*- coding: utf-8 -*-
"""
IfWe Phase 1 数据管道：导入 → 去噪 → 脱敏 → 会话化 → 入库 SQLite → 质量门禁
数据规范 v1 见 docs/IMPORT.md；CLI 入口在 scripts/import_chat.py（本模块作库被复用）

原则：
- Layer 0 原始 JSONL 只读，绝不修改
- 中档脱敏：库内保留细节；content_clean 为去噪+隐私脱敏后的分析/LLM 唯一输入
- 幂等：每次 --reset 全量重建（源自同一源文件，结果确定）
- 本项目不解析微信数据库、不提供任何抓取功能，仅处理用户已合法导出的文件
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config as cfg_mod

TZ = timezone(timedelta(hours=8))   # v1 固定东八区（与 config chat.timezone 默认值一致）

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = None        # 兼容占位：实际路径由 cfg_mod.resource_dir() 在调用时解析（打包安全）
SESSION_GAP_S = 30 * 60             # 可被 config chat.session_gap_minutes 覆盖（见 ingest 参数）

# ---------- 脱敏规则(中档) ----------
# 注意: 纯 "数字+号" 在聊天里最常是日期(几号), 不视为地址; 仅 栋/幢/单元/室/号楼 按地址处理
ADDR_SCOPED_LINE = re.compile(
    r"(?m)^\s*[^\n]{0,12}?(?:收货人|收件人|寄件人|收货地址|发货地址|退货地址|邮寄地址|所在地区|详细地址)\s*[:：]?[^\n]*"
)
PRIVACY_PATTERNS = [
    ("地址信息", ADDR_SCOPED_LINE, "[地址信息]"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证]"),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[银行卡]"),
    ("车牌", re.compile(r"(?<![\u4e00-\u9fa5A-Za-z])[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领][A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}(?![\u4e00-\u9fa5A-Za-z0-9])"), "[车牌]",),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱]"),
    ("地址", re.compile(r"(?<!\d)\d{1,6}(?:栋|幢|单元|室|号楼)(?!\d)"), "[地址]"),
]

_PRIVACY_PLACEHOLDERS = ("[地址信息]", "[手机号]", "[身份证]", "[银行卡]", "[车牌]", "[邮箱]", "[地址]")
QUOTE_PAT = re.compile(r"\[引用\s*([^\]]{1,200})\]", re.S)   # type=25 引用
AMOUNT_PAT = re.compile(r"[￥¥]\s*([0-9]+(?:\.[0-9]{1,2})?)")


# ---------- 数据解析 ----------
def load_raw(src_path: Path) -> list[dict]:
    """读 Layer0, 返回 list[dict]（消息体, 未修改源文件）

    v0.2 起逻辑迁入 importers.chatlab_jsonl（行为不变）；本函数保留为薄壳，
    兼容旧调用。新代码请用 importers.registry.auto_detect + Importer.parse。
    """
    from importers.chatlab_jsonl import ChatlabJsonlImporter
    return ChatlabJsonlImporter().parse(src_path)


def clean_text(text: str) -> str:
    """去噪: 统一换行/空白, 去掉首尾空白"""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def mask_privacy(text: str) -> tuple[str, bool]:
    """中档脱敏: 替换隐私模式为占位符, 返回 (脱敏文本, 是否命中)"""
    hit = False
    for _, pat, repl in PRIVACY_PATTERNS:
        if pat.search(text):
            text = pat.sub(repl, text)
            hit = True
    return text, hit


def parse_message(rec: dict, sender_map: dict[str, str]) -> dict:
    """单条消息 -> 规范化记录（type/subtype/字段抽取）
    sender_map: 原始账号名 -> A/B（来自 config 或 CLI 参数，绝不内置任何人）"""
    ts = int(rec["timestamp"])
    t = int(rec.get("type", -1))
    content = rec.get("content") or ""
    raw_clean, _ = mask_privacy(clean_text(content))
    out = {
        "message_id": str(rec.get("platformMessageId") or f"{ts}-{rec.get('_idx', 'x')}"),
        "ts": ts,
        "sender_key": sender_map.get(rec.get("accountName") or "", "X"),
        "sender_orig": rec.get("accountName"),
        "type": t,
        "subtype": None,
        "content_orig": content,
        "content_clean": raw_clean,
        "quoted_msg_id": None,
        "quoted_text": None,
        "amount": None,
        "attachment_name": None,
        "is_system": 0,
        "has_privacy": 0,
    }
    if t == 0:  # 文本
        out["subtype"] = "text"
    elif t == 7:  # 图片 / 表情 gif
        if content.strip() == "[图片]":
            out["subtype"] = "image"
        else:
            out["subtype"] = "emoji_gif"
            out["attachment_name"] = Path(content.replace("\\", "/")).name
        out["content_clean"] = ""  # 图片仅元数据
    elif t == 4:  # 文件
        out["subtype"] = "file"
        m = re.search(r"\[文件\]\s*(\S.*)", content)
        out["attachment_name"] = m.group(1).strip() if m else content
        out["content_clean"] = ""
    elif t == 23:  # 通话
        out["subtype"] = "call"
        out["content_clean"] = ""
    elif t == 24:  # 小程序
        out["subtype"] = "miniprogram"
        m = re.search(r"\[小程序\]\s*(\S.*)", content)
        out["attachment_name"] = m.group(1).strip() if m else content
        out["content_clean"] = ""
    elif t == 25:  # 文本+引用
        out["subtype"] = "quote_text"
        qm = QUOTE_PAT.search(content)
        if qm:
            out["quoted_text"] = qm.group(1).strip()
            main = content[: qm.start()].strip()
            out["content_clean"] = mask_privacy(clean_text(main))[0]
        else:
            out["content_clean"] = raw_clean
    elif t == 27:  # 名片
        out["subtype"] = "namecard"
        out["content_clean"] = ""
    elif t == 80:  # 撤回通知
        out["subtype"] = "recall"
        out["is_system"] = 1
    elif t == 99:  # 转账
        out["subtype"] = "transfer"
        am = AMOUNT_PAT.search(content)
        out["amount"] = float(am.group(1)) if am else None
        out["content_clean"] = ""
    else:
        out["subtype"] = "unknown"

    # 重新标记隐私: 命中任一占位符即视为命中
    out["has_privacy"] = 1 if any(p in out["content_clean"] for p in _PRIVACY_PLACEHOLDERS) else 0
    return out


def build_db(db_path: Path, schema_path: Path | None = None, no_reset: bool = False) -> sqlite3.Connection:
    if no_reset and db_path.exists():
        print(f"[i] 复用已有库: {db_path}")
        return sqlite3.connect(db_path)
    if schema_path is None:          # 打包后 schema 在只读资源目录（_MEIPASS/app）
        schema_path = cfg_mod.resource_dir() / "schema_v1.sql"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    return conn


def _resolve_importer(source: Path, importer):
    """v0.2 O-5a: 显式 importer 优先；否则 registry auto_detect；
    未命中回退 chatlab/WeFlow 解析并提示（保持 v0.1 可用性）。"""
    if importer is not None:
        return importer
    try:
        from importers.registry import auto_detect
    except ImportError:
        from importers.chatlab_jsonl import ChatlabJsonlImporter
        return ChatlabJsonlImporter()
    imp = auto_detect(source)
    if imp is None:
        from importers.chatlab_jsonl import ChatlabJsonlImporter
        print(f"[warn] 未自动识别源格式，回退 chatlab/WeFlow JSONL 解析: {Path(source).name}")
        imp = ChatlabJsonlImporter()
    else:
        print(f"[i] 识别源格式: {imp.source_name}")
    return imp


def ingest(source: Path, sender_map: dict[str, str], db_path: Path,
           session_gap_s: int = SESSION_GAP_S, no_reset: bool = False,
           importer=None) -> dict:
    """完整导入流水线：解析 → 脱敏 → 会话化 → 入库 → 门禁 → 报告。返回摘要 dict。

    importer: v0.2 可选，Importer 实例（见 importers/base.py）。缺省时按
    registry 自动探测格式，未命中回退 chatlab。既有调用（run.py /
    import_chat.py）零改动；返回 dict 结构不变。
    """
    imp = _resolve_importer(source, importer)
    raw = imp.parse(source)
    print(f"[1] 读取源消息: {len(raw)} 条（格式: {getattr(imp, 'source_name', '?')}）")
    if not raw:
        raise SystemExit("源文件里没有消息（_type=message）——请确认导出格式（docs/IMPORT.md）")

    msgs = [parse_message(r, sender_map) for r in raw]
    unknown_sender = sum(1 for m in msgs if m["sender_key"] == "X")
    unknown_type = sum(1 for m in msgs if m["subtype"] == "unknown")
    msgs.sort(key=lambda m: m["ts"])

    # 会话化
    sessions = []
    cur = [msgs[0]] if msgs else []
    for m in msgs[1:]:
        if m["ts"] - cur[-1]["ts"] > session_gap_s:
            if len(cur) >= 2:
                sessions.append(cur)
            cur = []
        cur.append(m)
    if len(cur) >= 2:
        sessions.append(cur)

    # 入库
    conn = build_db(db_path, no_reset=no_reset)
    now_utc = datetime.now(timezone.utc).isoformat()
    for i, m in enumerate(msgs):
        dt = datetime.fromtimestamp(m["ts"], TZ)
        m["ts_local"] = dt.isoformat()
        m["day"] = dt.strftime("%Y-%m-%d")
        m["_idx"] = i
    conn.executemany(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(m["message_id"], m["ts"], m["ts_local"], m["day"], m["sender_key"],
          m["sender_orig"], m["type"], m["subtype"], m["content_orig"],
          m["content_clean"], m["quoted_msg_id"], m["quoted_text"], m["amount"],
          m["attachment_name"], m["is_system"], m["has_privacy"]) for m in msgs],
    )

    cur_sessions = []
    for i, ses in enumerate(sessions):
        a = sum(1 for m in ses if m["sender_key"] == "A")
        b = len(ses) - a
        ttl = sum(len(m["content_clean"]) for m in ses)
        n_text = sum(1 for m in ses if m["subtype"] in ("text", "quote_text"))
        dt = datetime.fromtimestamp(ses[0]["ts"], TZ)
        cur_sessions.append((
            f"{dt.strftime('%Y%m%d')}-{ses[0]['ts']}-{i}",
            ses[0]["ts"], ses[-1]["ts"], dt.strftime("%Y-%m-%d"), len(ses), n_text,
            ttl, a, b, "A" if a > b else ("B" if b > a else "T"),
        ))
    conn.executemany("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)", cur_sessions)

    # 每日聚合
    day_rows: dict[str, list] = {}
    for m in msgs:
        d = day_rows.setdefault(m["day"], [0, 0, 0, 0, 0, 0, 0, 0])
        d[0] += 1                                  # msg_count
        if m["subtype"] in ("text", "quote_text"):
            d[1] += 1                              # text_count
        if m["type"] == 7:
            d[2] += 1                              # image_count
        if m["subtype"] == "recall":
            d[3] += 1                              # recall_count
        if m["subtype"] == "transfer":
            d[4] += 1                              # transfer_count
        d[5] += len(m["content_clean"])            # char_count
        d[6] += 1 if m["sender_key"] == "A" else 0
        d[7] += 1 if m["sender_key"] == "B" else 0
    session_count_by_day = Counter(s[3] for s in cur_sessions)
    cur_daily = []
    for day, d in sorted(day_rows.items()):
        cur_daily.append((day, d[0], d[1], d[2], d[3], d[4], session_count_by_day.get(day, 0), d[5], d[6], d[7]))
    conn.executemany("INSERT OR REPLACE INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?,?)", cur_daily)

    # 元数据
    sha = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    meta = [
        ("spec_version", "1"),
        ("source_file", Path(source).name),
        ("source_sha256", sha),
        ("imported_at", now_utc),
        ("session_gap_minutes", str(session_gap_s // 60)),
        ("sender_map", json.dumps(sender_map, ensure_ascii=False)),
    ]
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta)
    conn.commit()

    # 质量门禁 + 报告
    gates = run_gates(msgs, unknown_sender, unknown_type)
    summary = write_report(msgs, sessions, gates, us=unknown_sender, ut=unknown_type,
                           source=source, db_path=db_path)
    conn.close()
    print("\n[7] 门禁汇总:", json.dumps(gates, ensure_ascii=False, indent=2))
    ok = all(v.get("pass") for v in gates.values())
    summary["gates_all_pass"] = ok
    return summary


def run_gates(msgs, unknown_sender, unknown_type) -> dict:
    """结构性门禁（不内置任何人的统计预期）"""
    g = {}
    # 时间序(排序后必然非降, 防御性校验)
    g["G2_时间序列有序"] = {
        "check": "ts 非降(排序后)",
        "pass": all(msgs[i]["ts"] <= msgs[i + 1]["ts"] for i in range(len(msgs) - 1)),
    }
    ca = sum(1 for m in msgs if m["sender_key"] == "A")
    cb = sum(1 for m in msgs if m["sender_key"] == "B")
    total = len(msgs) or 1
    g["G3_双人占比"] = {
        "A": {"count": ca, "pct": round(ca / total * 100, 2)},
        "B": {"count": cb, "pct": round(cb / total * 100, 2)},
        "pass": (ca / total > 0.25) and (cb / total > 0.25),
    }
    # 断档 >30 天(排除首条与最后一条跨缝)
    gaps = []
    for i in range(1, len(msgs)):
        gap_days = (msgs[i]["ts"] - msgs[i - 1]["ts"]) / 86400
        if gap_days > 30:
            gaps.append((datetime.fromtimestamp(msgs[i - 1]["ts"], TZ).date().isoformat(),
                         datetime.fromtimestamp(msgs[i]["ts"], TZ).date().isoformat(),
                         round(gap_days, 1)))
    g["G4_长断档告警"] = {"count": len(gaps), "samples": gaps[:8], "pass": True}  # 告警不阻塞
    # 脱敏残留: 全量 content_clean 复扫(确定性, 非抽样)
    residue_rows = []
    for m in msgs:
        if not m["content_clean"]:
            continue
        if any(p.search(m["content_clean"]) for _, p, _ in PRIVACY_PATTERNS):
            residue_rows.append(m["message_id"])
    g["G5_脱敏残留"] = {"scanned": sum(1 for m in msgs if m["content_clean"]),
                        "residue": len(residue_rows), "pass": len(residue_rows) == 0}
    g["G7_未知发送者/类型"] = {"unknown_sender": unknown_sender, "unknown_type": unknown_type,
                              "pass": unknown_sender == 0 and unknown_type == 0}
    return g


def write_report(msgs, sessions, gates, us: int, ut: int,
                 source: Path, db_path: Path) -> dict:
    n_text = sum(1 for m in msgs if m["subtype"] in ("text", "quote_text"))
    n_img = sum(1 for m in msgs if m["type"] == 7)
    n_recall = sum(1 for m in msgs if m["subtype"] == "recall")
    chars = sum(len(m["content_clean"]) for m in msgs)
    day_min = min(m["day"] for m in msgs)
    day_max = max(m["day"] for m in msgs)
    all_pass = all(v["pass"] for v in gates.values())
    summary = {
        "message_count": len(msgs), "session_count": len(sessions),
        "active_days": len(set(m["day"] for m in msgs)),
        "text_count": n_text, "image_emoji_count": n_img, "recall_count": n_recall,
        "char_count_total": chars,
        "first_day": day_min, "last_day": day_max,
        "source_file": Path(source).name, "db": Path(db_path).name,
        "gates": {k: v["pass"] for k, v in gates.items()},
    }
    data_dir = db_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# IfWe Phase 1 数据管道 验收报告（数据规范 v1）",
        "",
        f"> 生成时间: {datetime.now(TZ).isoformat()}  |  源文件: {Path(source).name}  |  库: {Path(db_path).name}",
        "",
        "## 概览",
        "",
        f"- 消息总数: **{len(msgs)}**",
        f"- 时间跨度: {day_min} → {day_max}（{summary['active_days']} 个有消息日）",
        f"- 文本消息: {n_text} / 图片表情: {n_img} / 撤回: {n_recall}",
        f"- 文本内容字符合计: {chars} ｜ 会话数: {len(sessions)}",
        f"- 未知发送者: {us} / 未知类型: {ut}",
        "",
        f"**总体结论: {'全部通过 ✅' if all_pass else '存在未通过项 ❌'}**",
        "",
    ]
    (data_dir / "phase1_验收报告.md").write_text("\n".join(lines), encoding="utf-8")
    (data_dir / "phase1_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print("本模块是库；请用 CLI：python scripts/import_chat.py --help")
    sys.exit(2)
