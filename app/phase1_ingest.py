# -*- coding: utf-8 -*-
"""
IfWe Phase 1 数据管道：导入 → 去噪 → 脱敏 → 会话化 → 入库 SQLite → 质量门禁
数据规范 v2 见 docs/IMPORT.md；CLI 入口在 scripts/import_chat.py（本模块作库被复用）

原则：
- Layer 0 原始 JSONL 只读，绝不修改
- 中档脱敏：库内保留细节；content_clean 为去噪+隐私脱敏后的分析/LLM 唯一输入
- 幂等：每次入库从「全部已登记来源」全量重建（同源同参 → 结果确定）
- 本项目不解析微信数据库、不提供任何抓取功能，仅处理用户已合法导出的文件

v2（多源合并）：
- 一次导入可含多份来源文件（不同应用/工具导出），逐源各自映射 A/B，
  统一归一到 canonical 流后按时间戳全局合并排序，再做会话化与入库。
- 跨源去重：同一段对话从两个渠道导出时的重叠消息，用 dedup_key 去重。
- 来源登记：import_sources 表记录每份来源的存档、哈希、映射与条数；
  追加新来源时从全部已登记来源重建，语义干净且幂等。
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

# ---------- 数据规范版本 ----------
SPEC_VERSION = "2"

# 建库时按序执行的 DDL（v2 为增量：新表 + messages 补列，见 ensure_v2_schema）
SCHEMAS = ("schema_v1.sql", "schema_v2.sql")

# messages 表列序（入库一律用显式列名，避免 v1/v2 列序差异导致错位）
MESSAGE_COLS = (
    "message_id", "ts", "ts_local", "day", "sender_key", "sender_orig",
    "type", "subtype", "content_orig", "content_clean", "quoted_msg_id",
    "quoted_text", "amount", "attachment_name", "is_system", "has_privacy",
    "source_id", "dedup_key",
)


# ---------- v2 结构升级 / 跨源去重 ----------
def ensure_v2_schema(conn: sqlite3.Connection) -> None:
    """为 messages 补 v2 两列并建索引（幂等；旧库升级与新建库走同一条路径）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if not cols:                       # 表还不存在（尚未执行 v1 DDL）——交给上层先建表
        return
    if "source_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN source_id TEXT")
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN dedup_key TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_dedup  ON messages(dedup_key)")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_id_for(path: Path) -> str:
    """来源稳定 id：src-<源文件内容 sha256 前 16 位>。

    同一份文件重复导入 → 同一个 source_id（幂等）；文件变了 → id 也变。
    """
    return "src-" + file_sha256(path)[:16]


def dedup_key_for(ts: int, sender_key: str, content_clean: str,
                  attachment_name: str | None) -> str | None:
    """跨源去重键（同一段对话从两个渠道导出时的重叠消息）。

    只对**有确定性标识**的消息生成键：有文本用文本，无文本但有附件名用附件名。
    两者都没有（如纯 [图片] 且无文件名）返回 None —— 不做去重，也不猜测归类。
    """
    if content_clean:
        basis = f"t|{ts}|{sender_key}|{content_clean}"
    elif attachment_name:
        basis = f"a|{ts}|{sender_key}|{attachment_name}"
    else:
        return None
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


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


def parse_message(rec: dict, sender_map: dict[str, str],
                  source_id: str = "", seq: int | None = None) -> dict:
    """单条消息 -> 规范化记录（type/subtype/字段抽取）

    sender_map: 原始账号名 -> A/B（来自 config 或 CLI 参数，绝不内置任何人）
    source_id : v2 来源登记 id；给了就参与 message_id 前缀，保证跨源不撞号
    seq       : 该消息在**本来源内**的序号；仅当导出没有平台消息 id 时用于
                派生兜底 id（v1 用固定的 'x'，同一秒的多条消息会撞主键被静默覆盖）
    """
    ts = int(rec["timestamp"])
    t = int(rec.get("type", -1))
    content = rec.get("content") or ""
    raw_clean, _ = mask_privacy(clean_text(content))
    pid = rec.get("platformMessageId")
    if pid:
        mid = f"{source_id}:{pid}" if source_id else str(pid)
    else:                                   # 兜底 id：来源内序号，保证同秒不撞
        idx = rec.get("_idx")
        if idx is None:
            idx = seq if seq is not None else "x"
        mid = f"{source_id}:{ts}-{idx}" if source_id else f"{ts}-{idx}"
    out = {
        "message_id": mid,
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
        "source_id": source_id or None,
        "dedup_key": None,
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
    # v2 跨源去重键（subtype/字段抽取都做完后才能算）
    out["dedup_key"] = dedup_key_for(out["ts"], out["sender_key"],
                                     out["content_clean"], out["attachment_name"])
    return out


MEDIA_CARRY_COLS = ("media_id", "sha256", "filename", "kind", "ext", "mime",
                    "size_bytes", "width", "height", "rel_path", "added_at")


def _carry_over_media(db_path: Path) -> tuple[list, list]:
    """重建库前把媒体索引捞出来（重建后原样放回）。

    媒体是**独立于聊天记录**的一条通道：用户单独导入的表情包/图片库，
    不能因为「重新导入一次聊天记录」就凭空消失。所以媒体索引必须跨重建存活；
    而 message_media 的键是 message_id，未变动的来源其 id 是确定性的，关联依然有效。
    """
    if not db_path.exists():
        return [], []
    try:
        conn = sqlite3.connect(db_path)
        try:
            media = conn.execute(
                f"SELECT {','.join(MEDIA_CARRY_COLS)} FROM media").fetchall()
        except sqlite3.Error:
            media = []
        try:
            link = conn.execute(
                "SELECT message_id, media_id, link_by FROM message_media").fetchall()
        except sqlite3.Error:
            link = []
        conn.close()
        return media, link
    except sqlite3.Error:
        return [], []


def build_db(db_path: Path, schema_path: Path | None = None, no_reset: bool = False) -> sqlite3.Connection:
    """建/清库并按序应用 DDL（默认 SCHEMAS = v1 + v2 增量），最后补齐 v2 列。

    no_reset=True 时复用已有库（但仍会补结构，保证旧库可用）。
    全量重建会删掉库文件，但**媒体索引与消息↔媒体关联会被原样搬运回来**。
    """
    if no_reset and db_path.exists():
        print(f"[i] 复用已有库: {db_path}")
        conn = sqlite3.connect(db_path)
        ensure_v2_schema(conn)
        conn.commit()
        return conn
    res = cfg_mod.resource_dir()     # 打包后 schema 在只读资源目录（_MEIPASS/app）
    names = [Path(schema_path).name] if schema_path else list(SCHEMAS)
    media_rows, link_rows = _carry_over_media(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    for name in names:
        p = res / name
        if p.is_file():
            conn.executescript(p.read_text(encoding="utf-8"))
    ensure_v2_schema(conn)
    if media_rows:                                          # 媒体库跨重建存活
        conn.executemany(
            f"INSERT OR REPLACE INTO media ({','.join(MEDIA_CARRY_COLS)}) "
            f"VALUES ({','.join('?' * len(MEDIA_CARRY_COLS))})", media_rows)
        print(f"[i] 保留媒体索引 {len(media_rows)} 条（媒体库独立于聊天记录）")
    if link_rows:
        conn.executemany("INSERT OR REPLACE INTO message_media "
                         "(message_id,media_id,link_by) VALUES (?,?,?)", link_rows)
    conn.commit()
    return conn


def _resolve_importer(source: Path, importer, quiet: bool = False):
    """v0.2 O-5a: 显式 importer 优先；否则 registry auto_detect；
    未命中回退 chatlab/WeFlow 解析并提示（保持 v0.1 可用性）。
    quiet=True 时不打印（供合并预览复用，避免刷屏）。"""
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
        if not quiet:
            print(f"[warn] 未自动识别源格式，回退 chatlab/WeFlow JSONL 解析: {Path(source).name}")
        imp = ChatlabJsonlImporter()
    elif not quiet:
        print(f"[i] 识别源格式: {imp.source_name}")
    return imp


def _assemble(sources: list[dict], quiet: bool = False) -> dict:
    """解析全部来源 → 逐源映射 → 跨源去重 → 全局稳定排序 → 主键唯一化。

    只做内存装配，不碰数据库；ingest_many() 与 plan_merge() 共用同一份逻辑，
    保证「预览看到的合并结果」与「真正入库的结果」不会分叉。
    """
    if not sources:
        raise SystemExit("没有可导入的来源")

    msgs: list[dict] = []
    src_meta: list[dict] = []
    parsed_total = 0
    for si, s in enumerate(sources):
        path = Path(s["path"])
        imp = _resolve_importer(path, s.get("importer"), quiet=quiet)
        fmt = getattr(imp, "source_name", "?")
        raw = imp.parse(path)
        sid = s.get("source_id") or source_id_for(path)
        smap = s.get("sender_map") or {}
        parsed_total += len(raw)
        n_unknown = 0
        for i, rec in enumerate(raw):
            m = parse_message(rec, smap, source_id=sid, seq=i)
            m["_ord"] = len(msgs)
            m["_src"] = sid
            if m["sender_key"] == "X":
                n_unknown += 1
            msgs.append(m)
        tss = [int(r["timestamp"]) for r in raw if r.get("timestamp") is not None]
        src_meta.append({
            "source_id": sid, "name": (s.get("name") or path.name),
            "importer": fmt, "path": path, "sender_map": smap,
            "sha256": (s.get("sha256") or file_sha256(path)),
            "size_bytes": path.stat().st_size,
            "message_count": len(raw),
            "first_ts": min(tss) if tss else None,
            "last_ts": max(tss) if tss else None,
            "unknown_sender": n_unknown,
        })
        if not quiet and len(sources) > 1:
            print(f"[i] 来源 {si + 1}/{len(sources)}: {path.name} "
                  f"（{fmt}，{len(raw)} 条，映射 {smap or '∅'}）")

    if not quiet:
        fmts = " + ".join(dict.fromkeys(d["importer"] for d in src_meta))
        print(f"[1] 读取源消息: {parsed_total} 条（格式: {fmts}）")
    if not msgs:
        raise SystemExit("源文件里没有消息（_type=message）——请确认导出格式（docs/IMPORT.md）")

    # ---- 稳定排序（ts 为主键，全局出现序为次键）→ 跨源去重 ----
    msgs.sort(key=lambda m: (m["ts"], m["_ord"]))
    kept: list[dict] = []
    seen_dedup: set[str] = set()
    dup_by_source: Counter = Counter()
    for m in msgs:
        k = m["dedup_key"]
        if k is not None:
            if k in seen_dedup:
                dup_by_source[m["_src"]] += 1
                continue
            seen_dedup.add(k)
        kept.append(m)
    dup_removed = len(msgs) - len(kept)
    if dup_removed and not quiet:
        print(f"[i] 跨源去重: 合并前 {len(msgs)} 条 → 移除重复 {dup_removed} 条")
    msgs = kept

    # ---- 主键唯一化（同一来源内平台 id 重复时兜底，绝不静默覆盖丢消息）----
    seen_id: dict[str, int] = {}
    for m in msgs:
        mid = m["message_id"]
        n = seen_id.get(mid)
        if n is None:
            seen_id[mid] = 1
        else:
            seen_id[mid] = n + 1
            m["message_id"] = f"{mid}#{n + 1}"

    # ---- 映射一致性自查 ----
    # 去重之后，若同一「时间 + 内容」仍同时出现在 A 与 B 两侧，几乎只有一个解释：
    # 某份来源的 A/B 选反了。这时去重会静默失效（发送者不同 → 去重键不同），
    # 所以必须显式报出来，而不是等用户自己发现消息翻倍。
    key2senders: dict[tuple, set] = {}
    for m in msgs:
        if m["content_clean"]:
            key2senders.setdefault((m["ts"], m["content_clean"]), set()).add(m["sender_key"])
    map_conflicts = sum(1 for s in key2senders.values() if "A" in s and "B" in s)
    if map_conflicts and not quiet:
        print(f"[warn] 有 {map_conflicts} 组消息「时间+内容」相同但发送者相反"
              f"——请检查各来源的 A/B 是否选反了")

    return {"msgs": msgs, "src_meta": src_meta, "parsed_total": parsed_total,
            "dup_removed": dup_removed, "dup_by_source": dup_by_source,
            "map_conflicts": map_conflicts}


def plan_merge(sources: list[dict]) -> dict:
    """合并预览（只读不写库）：给 Web 向导展示「合并后到底会是什么样」。

    返回每条来源的条数/跨度、合并后的总条数与去重数、时间轴重叠区、
    以及每一方在各来源里的候选账号，供用户点选 A/B。
    """
    a = _assemble(sources, quiet=True)
    msgs, src_meta = a["msgs"], a["src_meta"]
    tss = [m["ts"] for m in msgs]
    # 时间轴重叠区：至少两份来源都覆盖到的时间范围（取「最晚的开始」→「最早的结束」）
    starts = [d["first_ts"] for d in src_meta if d["first_ts"] is not None]
    ends = [d["last_ts"] for d in src_meta if d["last_ts"] is not None]
    overlap = None
    if len(src_meta) > 1 and starts and ends:
        lo, hi = max(starts), min(ends)
        if lo <= hi:
            overlap = {"first_ts": lo, "last_ts": hi,
                       "days": round((hi - lo) / 86400, 1)}
    return {
        "message_count": len(msgs),
        "parsed_total": a["parsed_total"],
        "duplicates_removed": a["dup_removed"],
        "duplicates_by_source": dict(a["dup_by_source"]),
        "map_conflicts": a["map_conflicts"],
        "source_count": len(src_meta),
        "session_span": ({"first_ts": min(tss), "last_ts": max(tss),
                          "days": round((max(tss) - min(tss)) / 86400, 1)} if tss else None),
        "overlap": overlap,
        "sources": [{"source_id": d["source_id"], "name": d["name"],
                     "importer": d["importer"], "message_count": d["message_count"],
                     "first_ts": d["first_ts"], "last_ts": d["last_ts"],
                     "sender_map": d["sender_map"]} for d in src_meta],
        "unknown_sender": sum(1 for m in msgs if m["sender_key"] == "X"),
        "unknown_type": sum(1 for m in msgs if m["subtype"] == "unknown"),
    }


def ingest_many(sources: list[dict], db_path: Path,
                session_gap_s: int = SESSION_GAP_S, no_reset: bool = False) -> dict:
    """多来源合并导入：逐源解析 → 逐源 A/B 映射 → 跨源去重 → 全局按时间排序
    → 会话化 → 一次性重建入库 → 门禁 → 报告。返回摘要 dict。

    sources 每项：
        {"path": Path,                  # 源文件（只读）
         "sender_map": {原始账号名: "A"/"B"},   # **逐源**映射，不同应用账号名不同
         "source_id": str | None,       # 缺省按文件内容哈希派生（稳定、幂等）
         "name": str | None,            # 展示用文件名
         "importer": Importer | None}   # 缺省按注册表自动探测

    合并语义：先按 (ts, 全局出现序) 稳定排序，再对 dedup_key 相同的消息只保留
    第一条（跨源重复 → 记为 G8 告警，不阻塞）；无确定性标识的消息不去重。
    """
    if not sources:
        raise SystemExit("没有可导入的来源")

    a = _assemble(sources)
    msgs, src_meta = a["msgs"], a["src_meta"]
    parsed_total, dup_removed = a["parsed_total"], a["dup_removed"]
    dup_by_source = a["dup_by_source"]
    map_conflicts = a["map_conflicts"]

    unknown_sender = sum(1 for m in msgs if m["sender_key"] == "X")
    unknown_type = sum(1 for m in msgs if m["subtype"] == "unknown")

    # ---- 会话化（时间轴已是全局合并后的连续序列）----
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

    # ---- 入库 ----
    conn = build_db(db_path, no_reset=no_reset)
    now_utc = datetime.now(timezone.utc).isoformat()
    for i, m in enumerate(msgs):
        dt = datetime.fromtimestamp(m["ts"], TZ)
        m["ts_local"] = dt.isoformat()
        m["day"] = dt.strftime("%Y-%m-%d")
        m["_idx"] = i
    col_sql = ",".join(MESSAGE_COLS)
    conn.executemany(
        f"INSERT OR REPLACE INTO messages ({col_sql}) "
        f"VALUES ({','.join('?' * len(MESSAGE_COLS))})",
        [tuple(m.get(c) for c in MESSAGE_COLS) for m in msgs],
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

    # ---- 来源台账（v2）：写入本次参与重建的每一份来源 ----
    conn.executemany(
        "INSERT OR REPLACE INTO import_sources (source_id,name,importer,archive_path,"
        "sha256,size_bytes,sender_map,message_count,first_ts,last_ts,added_at,last_import_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(d["source_id"], d["name"], d["importer"], "",
          d["sha256"], d["size_bytes"], json.dumps(d["sender_map"], ensure_ascii=False),
          d["message_count"], d["first_ts"], d["last_ts"], now_utc, now_utc)
         for d in src_meta],
    )

    # ---- 元数据 ----
    meta = [
        ("spec_version", SPEC_VERSION),
        ("source_file", " + ".join(d["name"] for d in src_meta[:3])
         + (f" 等 {len(src_meta)} 份" if len(src_meta) > 3 else "")),
        ("source_count", str(len(src_meta))),
        ("source_ids", json.dumps([d["source_id"] for d in src_meta], ensure_ascii=False)),
        ("source_sha256", "" if len(src_meta) > 1 else
         (src_meta[0]["sha256"] if src_meta else "")),
        ("source_file_list", json.dumps([d["name"] for d in src_meta], ensure_ascii=False)),
        ("imported_at", now_utc),
        ("session_gap_minutes", str(session_gap_s // 60)),
        ("sender_map", json.dumps(
            {d["name"]: d["sender_map"] for d in src_meta} if len(src_meta) > 1
            else (src_meta[0]["sender_map"] if src_meta else {}), ensure_ascii=False)),
    ]
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta)
    conn.commit()

    # ---- 质量门禁 + 报告 ----
    gates = run_gates(msgs, unknown_sender, unknown_type,
                      dup_removed=dup_removed, parsed_total=parsed_total,
                      n_sources=len(src_meta), dup_by_source=dup_by_source,
                      map_conflicts=map_conflicts)
    summary = write_report(msgs, sessions, gates, us=unknown_sender, ut=unknown_type,
                           source=Path(src_meta[0]["path"]) if src_meta else db_path,
                           db_path=db_path, src_meta=src_meta, dup_removed=dup_removed)
    conn.close()
    print("\n[7] 门禁汇总:", json.dumps(gates, ensure_ascii=False, indent=2))
    ok = all(v.get("pass") for v in gates.values())
    summary["gates_all_pass"] = ok
    return summary


def ingest(source: Path, sender_map: dict[str, str], db_path: Path,
           session_gap_s: int = SESSION_GAP_S, no_reset: bool = False,
           importer=None) -> dict:
    """单来源导入（v0.2 兼容薄壳）：等价于 ingest_many([一份来源])。

    importer 缺省时按注册表自动探测格式，未命中回退 chatlab。既有调用
    （run.py / import_chat.py）零改动；返回 dict 结构与 v1 一致。
    """
    return ingest_many(
        [{"path": Path(source), "sender_map": sender_map,
          "importer": importer, "name": Path(source).name}],
        db_path, session_gap_s=session_gap_s, no_reset=no_reset)


def run_gates(msgs, unknown_sender, unknown_type, dup_removed: int = 0,
              parsed_total: int | None = None, n_sources: int = 1,
              dup_by_source: Counter | None = None,
              map_conflicts: int = 0) -> dict:
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
    # v2 跨源重复：同一份对话从多个渠道导出时的重叠消息（告警不阻塞）
    base = parsed_total if parsed_total is not None else (len(msgs) + dup_removed)
    g["G8_跨源重复"] = {
        "sources": n_sources, "parsed": base, "removed": dup_removed,
        "pct": round(dup_removed / (base or 1) * 100, 2),
        "by_source": dict(dup_by_source or {}),
        "map_conflicts": map_conflicts,     # >0 = 疑似某份来源的 A/B 选反了
        "pass": True,          # 告警不阻塞：重复本身是正常现象（同源重导/重叠导出）
    }
    return g


def write_report(msgs, sessions, gates, us: int, ut: int,
                 source: Path, db_path: Path, src_meta: list[dict] | None = None,
                 dup_removed: int = 0) -> dict:
    n_text = sum(1 for m in msgs if m["subtype"] in ("text", "quote_text"))
    n_img = sum(1 for m in msgs if m["type"] == 7)
    n_recall = sum(1 for m in msgs if m["subtype"] == "recall")
    chars = sum(len(m["content_clean"]) for m in msgs)
    day_min = min(m["day"] for m in msgs)
    day_max = max(m["day"] for m in msgs)
    all_pass = all(v["pass"] for v in gates.values())
    src_meta = src_meta or []
    if src_meta:
        src_label = " + ".join(d["name"] for d in src_meta[:3])
        if len(src_meta) > 3:
            src_label += f" 等 {len(src_meta)} 份"
    else:
        src_label = Path(source).name
    summary = {
        "message_count": len(msgs), "session_count": len(sessions),
        "active_days": len(set(m["day"] for m in msgs)),
        "text_count": n_text, "image_emoji_count": n_img, "recall_count": n_recall,
        "char_count_total": chars,
        "first_day": day_min, "last_day": day_max,
        "source_file": src_label, "db": Path(db_path).name,
        "source_count": len(src_meta) or 1,
        "duplicates_removed": dup_removed,
        "sources": [{"source_id": d["source_id"], "name": d["name"],
                     "importer": d["importer"], "message_count": d["message_count"],
                     "sender_map": d["sender_map"]} for d in src_meta],
        "gates": {k: v["pass"] for k, v in gates.items()},
        "gates_detail": gates,       # 完整门禁细节（去重条数、断档样本等）给界面用
    }
    data_dir = db_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# IfWe Phase 1 数据管道 验收报告（数据规范 v{SPEC_VERSION}）",
        "",
        f"> 生成时间: {datetime.now(TZ).isoformat()}  |  来源: {src_label}  |  库: {Path(db_path).name}",
        "",
        "## 来源",
        "",
    ]
    for d in src_meta:
        lines.append(f"- `{d['name']}`（{d['importer']}，{d['message_count']} 条，"
                     f"映射 {json.dumps(d['sender_map'], ensure_ascii=False)}）")
    if not src_meta:
        lines.append(f"- `{Path(source).name}`")
    lines += [
        "",
        "## 概览",
        "",
        f"- 消息总数: **{len(msgs)}**",
        f"- 时间跨度: {day_min} → {day_max}（{summary['active_days']} 个有消息日）",
        f"- 文本消息: {n_text} / 图片表情: {n_img} / 撤回: {n_recall}",
        f"- 文本内容字符合计: {chars} ｜ 会话数: {len(sessions)}",
        f"- 跨源去重移除: {dup_removed} 条",
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
