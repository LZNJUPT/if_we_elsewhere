# -*- coding: utf-8 -*-
"""
IfWe Phase 15 · 对话产品服务层（M2 + M3 + v0.3 桌面化）

把对话内核包成一个「对话推演」的本地 Web 服务：
  M3 聊天窗口 —— 打开就是对话框，不是仪表盘
  M2 任意节点切入 —— 在时间轴上选一天，写一句改写，开一条 IF 线
  v0.3 桌面化 —— 界面内分析（带进度/可中止）、人物档案、LLM 设置、
                 多好友（profile）隔离、首次运行引导

设计原则（沿用项目约定）:
  - 只读主库；一切对话写入 sim_* 隔离命名空间（DialEngine 保证）
  - 不公开部署：仅监听 127.0.0.1
  - 密钥只从环境变量或系统凭据管理器读（见 secret_store），不写明文文件
  - 零构建、无 CDN：原生 HTML/CSS/JS
  - 每个好友一个独立数据目录：data_dir()/persona_dir() 随 profile 切换

启动:
  python run.py server          # 项目根执行 → http://127.0.0.1:8015
  python run.py desktop         # 桌面窗口（desktop.py）
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

import config as cfg_mod
import phase1_ingest as p1
import phase2_llm
import phase4_retrieval
import phase5_common as pc
import profiles as profiles_mod
import secret_store
from doctor import run_doctor
from importers import registry as importer_registry
import import_sources
import media_store
from phase15_dial_engine import (AUTO_DAY_TURNS, get_default_start, DialEngine,
                                 _dial_lines, new_line)
from phase5_llm import get_client

# 静态资源：源码运行 = 仓库内 app/phase15_web；PyInstaller 冻结 = _MEIPASS/app/phase15_web
def _resolve_web_dir() -> Path:
    for cand in (cfg_mod.resource_dir() / "phase15_web",
                 Path(__file__).resolve().parent / "phase15_web"):
        if cand.is_dir():
            return cand
    return Path(__file__).resolve().parent / "phase15_web"


WEB_DIR = _resolve_web_dir()
PORT = int(os.environ.get("PORT") or cfg_mod.load()["defaults"]["port"])

# 真实表情包文件目录（如 WeFlow 导出的 Emojis 文件夹）；路径在 config media.emojis_dir 配置，
# 可用环境变量 IFWE_MEDIA_DIR 覆盖。未配置时 /api/media 返回 404，界面自动降级为占位符。
MEDIA_NAME = re.compile(r"^[0-9a-f]{32}\.(gif|jpg|jpeg|png)$", re.I)
MEDIA_MIME = {".gif": "image/gif", ".png": "image/png",
              ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

app = FastAPI(title="IfWe · 对话推演", version=cfg_mod.version())

_lock = threading.RLock()
_retriever = None
_engines: dict[str, DialEngine] = {}

# 无文本消息的占位（聊天记录展示用）
MEDIA_LABEL = {
    "image": "[图片]", "emoji_gif": "[表情包]", "file": "[文件]", "call": "[通话]",
    "miniprogram": "[小程序]", "namecard": "[名片]", "recall": "[撤回了一条消息]",
    "transfer": "[转账]", "quote_text": "",
}
DIMS5 = ("closeness", "conflict", "trust", "emotional_safety", "comm_quality")

TP_TYPE_META = {
    "gap": {"label": "断联", "tone": "warn"},
    "emotion": {"label": "情绪拐点", "tone": "info"},
    "event": {"label": "重要事件", "tone": "plain"},
    "activity_anomaly": {"label": "活跃高峰", "tone": "ok"},
    "other": {"label": "其他", "tone": "plain"},
}


SCHEMA_IF = cfg_mod.resource_dir() / "schema_if.sql"   # 兼容保留（已并入 pc.ALL_SCHEMAS）


_schema_ready: set[str] = set()          # 已建齐结构的库（切换好友/重建库时清空）


def _conn() -> sqlite3.Connection:
    """连当前好友的库并把结构建齐（幂等；首次访问某个库时才跑一次 DDL）"""
    conn = pc.connect()
    key = str(cfg_mod.db_path())
    if key not in _schema_ready:
        pc.apply_all_schemas(conn)
        _schema_ready.add(key)
    conn.commit()
    return conn


def _get_retriever():
    """记忆检索器全局单例（加载 embedding 与 onnx 较慢，只初始化一次）"""
    global _retriever
    with _lock:
        if _retriever is None:
            _retriever = phase4_retrieval.MemoryRetriever()
        return _retriever


def _get_engine(sim_id: str) -> DialEngine:
    with _lock:
        eng = _engines.get(sim_id)
        if eng is None:
            eng = DialEngine(_conn(), sim_id, client=get_client(),
                             retriever=_get_retriever(), verbose=False)
            _engines[sim_id] = eng
        return eng


def _drop_engine(sim_id: str) -> None:
    with _lock:
        eng = _engines.pop(sim_id, None)
    if eng is not None:
        try:
            eng.conn.close()
        except Exception:
            pass


# ================================================================ 全局互斥 + 后台任务状态（v0.3）
#
# analyze / import / profile 切换 / profile 删除 共用一把互斥锁：
#   - 长任务（导入 commit、分析）开始前取锁、结束释放；
#   - 分析跑在后台线程里，所以它只用锁做「启动登记」，运行期以 _ANALYZE["running"] 判定；
#   - 任何互斥操作在他人进行中一律 409，前端据此置灰按钮并给出原因。
_op_lock = threading.RLock()
_ANALYZE_LOCK = threading.Lock()
_ANALYZE: dict = {
    "running": False, "phase": "", "message": "", "started_at": 0.0,
    "finished_at": 0.0, "elapsed_s": 0.0, "cancel": False, "cancelled": False,
    "error": "", "last_result": None, "skip_llm": None, "profile": "", "job_id": "",
}
ANALYZE_STAGES = [
    ("1", "准备"), ("2", "事件抽取"), ("3", "记忆沉淀"),
    ("4", "关系状态"), ("5", "转折点"), ("6", "人格档案"),
]


def _analyze_running() -> bool:
    with _ANALYZE_LOCK:
        return bool(_ANALYZE["running"])


def _op_reason(exclude: str = "") -> str | None:
    """是否有互斥操作在进行中（None = 空闲）；返回给用户看的原因文案"""
    if exclude != "analyze" and _analyze_running():
        return "分析正在进行中"
    if exclude != "import":
        _purge_stale_import()          # 客户端中途放弃留下的过期锁先回收，否则会一直挡着
        if _lock_read() is not None:
            return "导入正在进行中"
    return None


def _require_idle(what: str) -> None:
    reason = _op_reason()
    if reason:
        raise HTTPException(409, f"{reason}，暂时不能{what}；请等它结束（或中止）后再试")


def _require_no_analyze(what: str) -> None:
    if _analyze_running():
        raise HTTPException(409, f"分析正在进行中，暂时不能{what}；"
                                 f"可在「分析」面板中止后再试")


def _drop_all_engines() -> None:
    for sid in list(_engines):
        _drop_engine(sid)


def _reset_caches() -> None:
    """好友切换 / 配置变更后：刷新检索器、对话引擎与路径缓存"""
    global _retriever
    with _lock:
        _retriever = None
    _drop_all_engines()
    try:
        pc.refresh_paths()
    except Exception:
        pass


# ---------------------------------------------------------------- API
@app.get("/api/health")
def api_health():
    key_env = cfg_mod.load()["llm"]["api_key_env"]
    return {"ok": True, "key": bool(cfg_mod.api_key()), "key_env": key_env,
            "port": PORT, "profile": cfg_mod.active_profile(),
            "busy": _op_reason(), "version": app.version}

@app.get("/api/state")
def api_state(line: str | None = None, tail: int = 200):
    """一次拉全：线列表 + 选中线的对话与状态（单用户本地，数据量小）"""
    with _lock:
        conn = _conn()
        lines = _dial_lines(conn)
        out_lines = []
        for ln in lines:
            sid = ln["sim_id"]
            n_msg = conn.execute("SELECT COUNT(*) FROM sim_messages WHERE sim_id=?",
                                 (sid,)).fetchone()[0]
            last = conn.execute("SELECT day FROM sim_messages WHERE sim_id=? "
                                "ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
            rel = conn.execute(
                "SELECT closeness, conflict, trust, emotional_safety, comm_quality, day "
                "FROM sim_rel_state WHERE sim_id=? ORDER BY day DESC LIMIT 1",
                (sid,)).fetchone()
            div = (ln["cfg"].get("divergence") or {}).get("rewrite")
            out_lines.append({
                "sim_id": sid, "name": ln["branch_name"], "start_day": ln["start_day"],
                "n_msg": n_msg, "cur_day": (last[0] if last else ln["start_day"]),
                "rewrite": div,
                "rel": ({"closeness": rel[0], "conflict": rel[1], "trust": rel[2],
                         "emotional_safety": rel[3], "comm_quality": rel[4],
                         "day": rel[5]} if rel else None),
            })

        if not out_lines:
            conn.close()
            return {"lines": [], "line": None, "messages": [], "rel_track": [],
                    "auto_day_turns": AUTO_DAY_TURNS}

        ids = [l["sim_id"] for l in out_lines]
        sid = line if line in ids else ids[0]
        rows = conn.execute(
            "SELECT day, sender, content, meta, emotion_s FROM sim_messages WHERE sim_id=? "
            "ORDER BY rowid ASC", (sid,)).fetchall()
        messages = []
        for day, sender, content, meta, emo in rows[-tail:]:
            try:
                m = json.loads(meta or "{}")
            except Exception:
                m = {}
            messages.append({"day": day, "sender": sender, "content": content,
                             "medium": m.get("medium", "text"),
                             "action": m.get("action", ""),
                             "sticker": m.get("sticker"),
                             "silent": bool(m.get("silent")),
                             "willingness": m.get("willingness"),
                             "human": bool(m.get("human")), "emotion": emo or ""})
        track = []
        for d, c, cf, t, e, q in conn.execute(
                "SELECT day, closeness, conflict, trust, emotional_safety, comm_quality "
                "FROM sim_rel_state WHERE sim_id=? ORDER BY day", (sid,)):
            track.append({"day": d, "closeness": c, "conflict": cf, "trust": t,
                          "emotional_safety": e, "comm_quality": q})
        conn.close()
        return {"lines": out_lines, "line": sid, "messages": messages,
                "rel_track": track, "auto_day_turns": AUTO_DAY_TURNS}


def _decision_points(conn) -> list[dict]:
    """岔路口标记：从用户自己的 turning_points 表动态生成（不内置任何人的事件）。
    analyze/demo 阶段写入 turning_points；这里只读。"""
    try:
        rows = conn.execute(
            "SELECT tp_id, day, title, description FROM turning_points ORDER BY day").fetchall()
    except Exception:
        rows = []
    return [{"id": r[0], "day": r[1] or "", "label": r[2] or "", "note": r[3] or ""}
            for r in rows if r[1]]


@app.get("/api/anchors")
def api_anchors():
    """时间轴：转折点 + 月度消息量 + 逐月关系状态 + 岔路口标记（全部来自用户自己的库）"""
    conn = _conn()
    tps = []
    for tp_id, day, rng, ttype, title, desc in conn.execute(
            "SELECT tp_id, day, date_range, tp_type, title, description FROM turning_points "
            "ORDER BY day"):
        meta = TP_TYPE_META.get(ttype, TP_TYPE_META["other"])
        tps.append({"id": tp_id, "day": day, "range": rng, "type": ttype,
                    "type_label": meta["label"], "tone": meta["tone"],
                    "title": title, "desc": desc})
    monthly = [{"month": m, "n": n} for m, n in conn.execute(
        "SELECT substr(day,1,7) AS m, COUNT(*) FROM messages WHERE day IS NOT NULL "
        "GROUP BY m ORDER BY m")]
    rel = [{"period": p, "closeness": c, "trust": t, "emotional_safety": e,
            "comm_quality": q, "conflict": cf, "confidence": conf}
           for p, c, cf, t, e, q, conf in conn.execute(
               "SELECT period, closeness, conflict, trust, emotional_safety, comm_quality, "
               "confidence FROM relationship_state ORDER BY period")]
    span = conn.execute("SELECT MIN(day), MAX(day) FROM messages WHERE day IS NOT NULL"
                        ).fetchone()
    n_all = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    dps = _decision_points(conn)
    default_start = get_default_start(conn)
    conn.close()
    return {"posts": tps, "monthly": monthly, "rel": rel,
            "first_day": span[0], "last_day": span[1], "n_messages": n_all,
            "decision_points": dps, "default_start": default_start}


# 可展示的消息：有文本，或有可占位的媒介类型
# （必须下沉到 SQL 过滤，否则 offset 与返回行数不一致，翻页会错位/重复）
SHOWABLE = ("((content_clean IS NOT NULL AND TRIM(content_clean) <> '') OR subtype IN "
            "('image','emoji_gif','file','call','miniprogram','namecard','recall','transfer'))")


def _real_rows(conn, where: str, params: tuple, limit: int, offset: int, desc: bool = True):
    """真实聊天记录取数 → 展示用条目（只取 content_clean，不含原文 PII）"""
    # 必须带 message_id 作为次级排序键：同一秒常有多条消息，只按 ts 排序时
    # SQLite 对相同键的顺序不稳定，会导致分页重叠/漏条（Phase 15 实测）
    d = "DESC" if desc else "ASC"
    rows = conn.execute(
        "SELECT day, ts_local, sender_key, subtype, content_clean, attachment_name, amount "
        f"FROM messages WHERE ({where}) AND {SHOWABLE} "
        f"ORDER BY ts {d}, message_id {d} LIMIT ? OFFSET ?",
        params + (limit, offset)).fetchall()
    if desc:
        rows = list(reversed(rows))
    out = []
    for day, ts_local, sender, subtype, text, att, amt in rows:
        t = (text or "").strip()
        if not t:
            if subtype == "transfer" and amt:
                t = f"[转账 ¥{amt:g}]"
            elif subtype == "file" and att:
                t = f"[文件] {att}"
            else:
                t = MEDIA_LABEL.get(subtype or "", "") or "[消息]"
        out.append({"day": day, "time": (ts_local or "")[11:16], "sender": sender,
                    "subtype": subtype or "text", "text": t,
                    "media": att if (att and MEDIA_NAME.match(att)) else None})
    return out


def _sniff_mime(p: Path) -> str:
    """按文件实际内容判断类型 —— 导出文件常出现扩展名与实际格式不符（如 .jpg 实为 PNG）"""
    try:
        with p.open("rb") as f:
            head = f.read(12)
    except Exception:
        return "application/octet-stream"
    if head[:4] == b"GIF8":
        return "image/gif"
    if head[:4] == b"\x89PNG":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return MEDIA_MIME.get(p.suffix.lower(), "application/octet-stream")


@app.get("/api/media/{name}")
def api_media(name: str):
    """媒体文件（表情包 / 图片）。

    v0.3 解析顺序：
      1. 先查媒体索引表（自己导入的表情包 / 图片，任意文件名都行）；
      2. 再回退旧的 `config media.emojis_dir`（32 位 hex 命名的历史约定）。
    文件名做了净化，且索引表里的路径由我们自己生成 → 无路径穿越。
    """
    n = media_store.safe_name(name)
    if not n:
        raise HTTPException(400, "非法文件名")
    p = media_store.resolve(n)
    if p is not None:
        return FileResponse(str(p), media_type=_sniff_mime(p))
    if MEDIA_NAME.match(n):                      # 兼容旧配置：WeFlow 导出的 32 位 hex 命名
        base = cfg_mod.emojis_dir()
        if base is not None:
            legacy = base / n
            try:
                if legacy.is_file():
                    return FileResponse(str(legacy), media_type=_sniff_mime(legacy))
            except Exception:
                pass
    raise HTTPException(404, "图片不在媒体库里（可在「媒体导入」里把它加进来）")


@app.get("/api/history")
def api_history(line: str, limit: int = 40, offset: int = 0):
    """该线起点【之前】的真实聊天记录（分页；用于沉浸与回忆，时间正序返回）"""
    conn = _conn()
    row = conn.execute("SELECT start_day, divergence_point, rewritten_choice "
                       "FROM sim_runs WHERE sim_id=?", (line,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "线不存在")
    start_day, div, rewrite = row
    limit = max(1, min(200, limit))
    total = conn.execute(f"SELECT COUNT(*) FROM messages WHERE day < ? AND {SHOWABLE}",
                         (start_day,)).fetchone()[0]
    items = _real_rows(conn, "day < ?", (start_day,), limit, max(0, offset), desc=True)
    span = conn.execute(f"SELECT MIN(day) FROM messages WHERE day < ? AND {SHOWABLE}",
                        (start_day,)).fetchone()[0]
    conn.close()
    return {"total": total, "offset": max(0, offset), "limit": limit, "items": items,
            "start_day": start_day, "first_day": span, "divergence": div, "rewrite": rewrite}


@app.get("/api/recall")
def api_recall(month: str, limit: int = 14):
    """某月的真实对话片段（时间轴选点时的"想起来了吗"预览）"""
    conn = _conn()
    n = conn.execute(f"SELECT COUNT(*) FROM messages WHERE day LIKE ? AND {SHOWABLE}",
                     (month + "%",)).fetchone()[0]
    days = [r[0] for r in conn.execute(
        f"SELECT day FROM messages WHERE day LIKE ? AND {SHOWABLE} "
        "GROUP BY day ORDER BY COUNT(*) DESC LIMIT 3", (month + "%",))]
    items = _real_rows(conn, "day LIKE ?", (month + "%",), limit, 0, desc=False)
    conn.close()
    return {"month": month, "n": n, "busy_days": days, "items": items}


class NewLineBody(BaseModel):
    name: str = ""
    start: str = ""                # 空 = 运行时取「最后一条消息次日」（见 api_new_line）
    divergence: str | None = None
    rewrite: str | None = None


@app.post("/api/lines")
def api_new_line(body: NewLineBody):
    with _lock:
        conn = _conn()
        default_start = get_default_start(conn)
        conn.close()
    start = (body.start or default_start).strip()
    if len(start) == 7:            # 允许只填到月（如 2025-06）
        start = start + "-01"
    name = (body.name or "").strip() or f"从 {start} 重新开始"
    rewrite = (body.rewrite or "").strip() or None
    divergence = (body.divergence or start).strip() if rewrite else None
    with _lock:
        conn = _conn()
        try:
            sim_id = new_line(conn, start_day=start, name=name,
                              divergence=divergence, rewrite=rewrite)
        except Exception as e:
            conn.close()
            raise HTTPException(500, f"建线失败: {str(e)[:200]}")
        conn.close()
    _drop_engine(sim_id)
    return {"sim_id": sim_id, "name": name, "start": start}


class SayBody(BaseModel):
    line: str
    text: str


@app.post("/api/say")
def api_say(body: SayBody):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "消息为空")
    if not cfg_mod.load()["privacy"]["allow_llm_send"]:
        raise HTTPException(403, "已在 config.yaml 中关闭外发（privacy.allow_llm_send=false）；"
                                 "如需对话推演，请显式开启并自行确认隐私边界")
    if not cfg_mod.api_key():
        key_env = cfg_mod.load()["llm"]["api_key_env"]
        raise HTTPException(400, f"缺少 LLM API Key：请在界面「设置」里填写并保存，"
                                 f"或设置环境变量 {key_env} 后重启服务")
    try:
        eng = _get_engine(body.line)
        res = eng.say(text)
    except Exception as e:
        _drop_engine(body.line)
        raise HTTPException(500, f"生成失败: {str(e)[:300]}")
    return {"day": res["day"], "reply": res["reply"], "medium": res["medium"],
            "action": res["action"], "sticker": res.get("sticker"),
            "event_note": res.get("event_note"),
            "silent": bool(res.get("silent")),
            "willingness": res.get("willingness"),
            "rel": res.get("rel"), "advanced_to": res.get("advanced_to")}


class DayBody(BaseModel):
    line: str
    n: int = 1


@app.post("/api/day")
def api_day(body: DayBody):
    try:
        eng = _get_engine(body.line)
        day = eng.advance_day(max(1, min(30, body.n)))
    except Exception as e:
        _drop_engine(body.line)
        raise HTTPException(500, f"推进失败: {str(e)[:200]}")
    return {"day": day}


@app.delete("/api/lines/{sim_id}")
def api_delete_line(sim_id: str):
    _drop_engine(sim_id)
    with _lock:
        conn = _conn()
        for t in ("sim_messages", "sim_sessions", "sim_events", "sim_facts",
                  "sim_rel_state", "sim_pcc_log", "sim_working_mem"):
            conn.execute(f"DELETE FROM {t} WHERE sim_id=?", (sim_id,))
        conn.execute("DELETE FROM ifr_branch WHERE sim_id=?", (sim_id,))
        conn.execute("DELETE FROM sim_runs WHERE sim_id=?", (sim_id,))
        conn.commit()
        conn.close()
    return {"ok": True}


# ================================================================ Web 导入向导（v0.3 · 多源合并）
#
# 生命周期：upload（多文件落临时目录，记为一个 batch + 取 .lock）
#        → preview（逐份体检 + 合并预览 + 逐源选 A/B）
#        → merge（改了 A/B 后重算合并预览，只读不写库）
#        → commit（源存档 → 从**全部已登记来源**重建库 → 媒体关联）
#        → 链路结束（含异常路径）立即删除临时文件与锁。
#
# v2 关键语义：库永远是「全部已登记来源」重建出来的结果。追加一份新来源不会丢掉
# 旧来源 —— 这是「不同应用各自导出、再按时间自动合并」的基础。来源存档与登记表见
# import_sources.py，界面可查看 / 单独移除任一来源。移除来源同样会触发一次重建。
#
# 安全：仅本机 127.0.0.1；响应不含服务器路径；白名单扩展名 + 单份 ≤50MB +
#       单批 ≤12 份 + batch_id/import_id 严格 hex 校验 + 同时仅一个导入任务。
IMPORT_MAX_BYTES = 50 * 1024 * 1024
IMPORT_MAX_FILES = 12
IMPORT_REQUEST_MAX_BYTES = 256 * 1024 * 1024
IMPORT_ALLOWED_EXTS = {".jsonl", ".json", ".csv", ".txt", ".md", ".log", ".docx", ".pdf"}
IMPORT_EXTS_TEXT = ".jsonl / .json / .csv / .txt / .md / .log / .docx（PDF 会给出转存引导）"
IMPORT_STALE_S = 3600                                    # 锁/临时文件过期回收阈值
_import_mutex = _op_lock                                 # 与分析/好友切换共用同一把互斥锁
MEDIA_MAX_FILES = 300
MEDIA_REQUEST_MAX_BYTES = 256 * 1024 * 1024


def _tmp_root() -> Path:
    """导入临时根目录（随当前好友的数据目录走；data/ 已 gitignore，绝不入库）"""
    return cfg_mod.data_dir() / "tmp_import"


def _import_tmp_dir() -> Path:
    return _tmp_root()


def _media_tmp_dir() -> Path:
    return _tmp_root() / "media"


class ImportRefBody(BaseModel):
    """定位参数：新接口用 batch_id；兼容旧的单文件 import_id。

    exclude：本批里被用户剔除的 import_id（前端「移除这份」按钮），不参与预览/合并/导入。
    extra="forbid"：字段名写错要当场 422。这里被坑过一次 —— 少一个字面拼写差异
    会让 pydantic 静默忽略整段字段，问题一路漂到用户面前才以「莫名其妙」的形式出现。
    """
    model_config = ConfigDict(extra="forbid")
    batch_id: str | None = None
    import_id: str | None = None
    exclude: list[str] = []


class ImportPreviewBody(ImportRefBody):
    pass


class ImportSourceSel(BaseModel):
    """一份来源的 A/B 映射。

    `sender_a/sender_b` 与 `a/b` 两种写法都收：界面一度发的是 `{import_id, a, b}`，
    pydantic 默认忽略未知字段 → 映射被读成空串 → 用户看到的却是
    「「telegram_result.json」还没选 A/B 双方账号」（2026-09-14 实测复现）。
    现在显式收下两种写法，其余字段一律 422。
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    import_id: str
    sender_a: str = Field("", validation_alias=AliasChoices("sender_a", "a"))
    sender_b: str = Field("", validation_alias=AliasChoices("sender_b", "b"))


class ImportMergeBody(ImportRefBody):
    sources: list[ImportSourceSel] = []


class ImportCommitBody(ImportRefBody):
    sender_a: str | None = None          # 单源旧调用兼容
    sender_b: str | None = None
    session_gap_minutes: int | None = None
    sources: list[ImportSourceSel] = []


# ---------------------------------------------------------------- 锁 / 批次
def _hex32(value: str | None) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value or ""):
        raise HTTPException(400, "非法 id")
    return value or ""


def _batch_read() -> dict | None:
    try:
        d = json.loads((_import_tmp_dir() / ".lock").read_text(encoding="utf-8"))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _lock_read() -> dict | None:                 # 兼容旧的调用名
    return _batch_read()


def _lock_write(meta: dict) -> None:
    d = _import_tmp_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / ".lock").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _import_file(import_id: str) -> Path:
    """按 import_id 取回某个临时文件（import_id 严格 hex，杜绝路径穿越）。"""
    _hex32(import_id)
    tmp_dir = _import_tmp_dir()
    if not tmp_dir.is_dir():
        raise HTTPException(404, "临时文件不存在（已过期或已完成）")
    files = sorted(tmp_dir.glob(import_id + ".*"))
    if not files:
        raise HTTPException(404, "临时文件不存在（已过期或已完成）")
    return files[0]


def _import_cleanup(import_ids: list[str] | None = None) -> None:
    """删 .lock + 指定 import_id 的临时文件；import_ids=None 时清空临时目录。

    本机 safe-delete 对目录级删除 fail-closed，所以一律逐文件 unlink。
    """
    tmp_dir = _import_tmp_dir()
    try:
        (tmp_dir / ".lock").unlink()
    except OSError:
        pass
    if not tmp_dir.is_dir():
        return
    if import_ids is None:
        for p in tmp_dir.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass
        return
    for iid in import_ids:
        for p in tmp_dir.glob(f"{iid}.*"):
            try:
                p.unlink()
            except OSError:
                pass


def _purge_stale_import() -> None:
    """回收超时锁与孤儿临时文件（客户端中途放弃的兜底）。"""
    tmp_dir = _import_tmp_dir()
    lockp = tmp_dir / ".lock"
    if lockp.exists() and time.time() - lockp.stat().st_mtime > IMPORT_STALE_S:
        _import_cleanup()
    if tmp_dir.is_dir():
        for p in tmp_dir.iterdir():
            if p.is_file() and p.name != ".lock" and \
                    time.time() - p.stat().st_mtime > IMPORT_STALE_S:
                try:
                    p.unlink()
                except OSError:
                    pass


def _resolve_batch(body: ImportRefBody) -> tuple[str, dict]:
    """按 batch_id 或批内任一 import_id 定位当前批次。"""
    lock = _batch_read()
    if not lock:
        raise HTTPException(409, "导入会话不存在或已失效，请重新上传")
    files = lock.get("files") or []
    given = ((body.batch_id or body.import_id) or "").strip()
    if given != lock.get("batch_id") and not any(f.get("import_id") == given for f in files):
        raise HTTPException(409, "导入会话不存在或已失效，请重新上传")
    return str(lock.get("batch_id") or ""), lock


def _extract_uploads(request: Request, body: bytes) -> list[tuple[bytes, str]]:
    """支持 multipart/form-data（**可含多个文件段**，手写解析零依赖）与裸 octet-stream。"""
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("multipart/"):
        return [(body, request.query_params.get("filename", ""))] if body else []
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not m:
        raise HTTPException(400, "multipart 缺少 boundary")
    sep = b"--" + m.group(1).encode()
    out: list[tuple[bytes, str]] = []
    for seg in body.split(sep)[1:]:
        if seg.strip(b"\r\n-") == b"" or b"\r\n\r\n" not in seg:
            continue
        head, _, payload = seg.partition(b"\r\n\r\n")
        fm = re.search(r'filename="([^"]*)"',
                       head.decode("utf-8", "replace"))
        if not fm:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        out.append((payload, fm.group(1)))
    if not out:
        raise HTTPException(400, "multipart 中未找到文件字段")
    return out


def _ts_pair(span: dict | None) -> tuple[int | None, int | None]:
    """doctor 的 time_span（ISO 字符串）→ (first_ts, last_ts) 秒级整数。"""
    if not span:
        return None, None
    from datetime import datetime as _dt

    def _p(v):
        try:
            return int(_dt.fromisoformat(v).timestamp())
        except Exception:
            return None
    return _p(span.get("first")), _p(span.get("last"))


def _batch_files(lock: dict, exclude: list[str] | None = None) -> list[dict]:
    """批次里参与本次操作的文件（剔除被用户移除的）。"""
    ex = set(exclude or [])
    return [f for f in (lock.get("files") or []) if f.get("import_id") not in ex]


def _sel_map(lock: dict, body: ImportCommitBody) -> dict[str, tuple[str, str]]:
    """逐源 A/B 映射：优先 body.sources；否则把 body.sender_a/b 套用到全部来源。"""
    files = _batch_files(lock, body.exclude)
    sel: dict[str, tuple[str, str]] = {}
    for s in (body.sources or []):
        sel[s.import_id] = ((s.sender_a or "").strip(), (s.sender_b or "").strip())
    if not sel and (body.sender_a or body.sender_b):
        pair = ((body.sender_a or "").strip(), (body.sender_b or "").strip())
        for f in files:
            sel[f["import_id"]] = pair
    return sel


def _batch_specs(lock: dict, sel: dict[str, tuple[str, str]],
                 exclude: list[str] | None = None) -> list[dict]:
    """把批次里的临时文件拼成 ingest_many 的输入（含逐源映射校验）。"""
    specs = []
    for f in _batch_files(lock, exclude):
        iid = f["import_id"]
        a, b = sel.get(iid, ("", ""))
        if not a or not b:
            raise HTTPException(400, f"「{f['filename']}」还没选 A/B 双方账号")
        if a == b:
            raise HTTPException(400, f"「{f['filename']}」的你/对方不能是同一个账号")
        specs.append({"path": _import_file(iid), "sender_map": {a: "A", b: "B"},
                      "name": f["filename"], "importer": None})
    return specs


# ---------------------------------------------------------------- 上传
@app.post("/api/import/upload")
async def api_import_upload(request: Request):
    """接收一批来源文件（1~12 份，可来自不同应用/格式），落临时目录并取锁。"""
    _require_no_analyze("导入记录")
    raw = await request.body()
    with _import_mutex:
        _purge_stale_import()
        # 上一个批次多半是「被放弃」的：用户关掉向导、刷新页面、或预览面板重载 —— 请求
        # 没走完，服务端的锁就留在那儿。本机单用户场景下，**新上传直接接管**，而不是让
        # 用户对着 409 干等一小时（用户可以接受「新上传覆盖旧批次」，不能接受「卡住」）。
        stale = _batch_read()
        reclaimed = False
        if stale is not None:
            old_ids = [f.get("import_id") for f in (stale.get("files") or [])]
            _import_cleanup(old_ids or None)
            reclaimed = True
            print(f"[i] 回收上一个未完成的导入批次（{len(old_ids)} 份临时文件）")
        if len(raw) > IMPORT_REQUEST_MAX_BYTES:
            raise HTTPException(413, "本次上传体积过大，请分批导入")
        items = _extract_uploads(request, raw)
        if len(items) > IMPORT_MAX_FILES:
            raise HTTPException(400, f"单次最多 {IMPORT_MAX_FILES} 份文件；"
                                     f"更多请分两批导入（第二批会与已导入的来源自动合并）")
        tmp_dir = _import_tmp_dir()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        batch_id = uuid.uuid4().hex
        files: list[dict] = []
        try:
            seen: set[str] = set()
            for data, filename in items:
                name = safe_filename(filename)
                ext = Path(name).suffix.lower()
                if not data:
                    raise HTTPException(400, f"「{name or '未命名文件'}」内容为空")
                if len(data) > IMPORT_MAX_BYTES:
                    raise HTTPException(413, f"「{name}」超过单份 50MB 上限")
                if ext not in IMPORT_ALLOWED_EXTS:
                    raise HTTPException(400, f"「{name}」格式不支持：仅接受 {IMPORT_EXTS_TEXT}")
                if name in seen:
                    raise HTTPException(400, f"本批里有重名文件「{name}」，请先改名再上传")
                seen.add(name)
                iid = uuid.uuid4().hex
                (tmp_dir / (iid + ext)).write_bytes(data)
                files.append({"import_id": iid, "filename": name, "ext": ext,
                              "size": len(data)})
            _lock_write({"batch_id": batch_id, "files": files, "created": time.time()})
        except BaseException:
            _import_cleanup([f["import_id"] for f in files])
            raise
        return {"batch_id": batch_id, "files": files, "count": len(files),
                "reclaimed": reclaimed,
                "import_id": files[0]["import_id"] if len(files) == 1 else None}


def safe_filename(name: str) -> str:
    return Path((name or "").replace("\\", "/")).name.strip()[:120]


@app.post("/api/import/cancel")
def api_import_cancel():
    """放弃当前批次：释放锁并删除临时文件。

    向导的「← 重新上传」和关闭弹窗都会调它。没有这个接口的话，用户上传后直接
    关掉向导会留下锁，之后整整一小时（IMPORT_STALE_S）都无法再上传新文件。
    """
    with _import_mutex:
        lock = _batch_read()
        ids = [f.get("import_id") for f in (lock.get("files") or [])] if lock else []
        _import_cleanup(ids or None)
        return {"ok": True, "released": bool(lock), "files": len(ids)}


# ---------------------------------------------------------------- 预览
@app.post("/api/import/preview")
def api_import_preview(body: ImportPreviewBody):
    """逐份体检（格式/条数/跨度/候选账号/脱敏预估/样例）+ 合并预览（暂用体检建议映射）。

    单个文件认不出来 → **保留锁**，让用户在第二步点「移除本份」后继续（这是可操作状态）。
    请求整体出错（服务端自身异常）→ **释放锁**，否则用户会被一个自己无法解除的锁
    挡在门外（表现为「导入正在进行中」且怎么点都没用）。
    """
    with _import_mutex:
        _bid, lock0 = _resolve_batch(body)
        ids0 = [f.get("import_id") for f in (lock0.get("files") or [])]
        try:
            _bid, lock = _resolve_batch(body)
            items = []
            sel: dict[str, tuple[str, str]] = {}
            for f in _batch_files(lock, body.exclude):
                src = _import_file(f["import_id"])
                try:
                    report = run_doctor(src)
                except Exception as e:
                    report = {"source_file": f["filename"], "recognized": False,
                              "importable": False, "reasons": [f"体检失败：{str(e)[:160]}"],
                              "candidates": [], "advice": []}
                candidates = report.get("candidates") or []
                _attach_samples(src, report, candidates)
                # 默认映射：config 里登记过的「我/对方」优先，其次才是体检的统计猜测
                pair, src_of = None, ""
                pref = _preferred_mapping(candidates)
                if pref:
                    pair, src_of = (pref["sender_a"], pref["sender_b"]), "config"
                else:
                    sug = report.get("suggested_args") or {}
                    if sug.get("sender_a") and sug.get("sender_b"):
                        pair, src_of = (sug["sender_a"], sug["sender_b"]), "guess"
                if pair:
                    report["mapping_default"] = {"sender_a": pair[0], "sender_b": pair[1],
                                                 "source": src_of}
                    sel[f["import_id"]] = pair
                items.append({"import_id": f["import_id"], "filename": f["filename"],
                              "size": f["size"], "report": report,
                              "candidates": candidates})
            # 合并预览：只用能识别出格式、且能凑出两个候选账号的来源
            merge, merge_error = None, ""
            try:
                specs = []
                for it in items:
                    if not (it["report"].get("recognized")
                            and it["report"].get("message_count")):
                        continue
                    pair = sel.get(it["import_id"])
                    if not pair:
                        continue
                    specs.append({"path": _import_file(it["import_id"]),
                                  "sender_map": {pair[0]: "A", pair[1]: "B"},
                                  "name": it["filename"], "importer": None})
                if specs:
                    merge = p1.plan_merge(specs)
                    merge["provisional"] = True   # 用的是体检建议映射，改选后走 /merge 重算
            except (SystemExit, Exception) as e:     # SystemExit 不是 Exception 子类
                merge_error = str(e)[:200]
            return {"batch_id": lock.get("batch_id"), "items": items,
                    "merge": merge, "merge_error": merge_error,
                    "registered": import_sources.list_view(),
                    "media": _media_stats_safe()}
        except HTTPException:
            raise
        except BaseException as e:
            _import_cleanup(ids0)
            raise HTTPException(500, f"预览失败（已释放导入会话，可重试）：{str(e)[:200]}")


def _match_names(v) -> list[str]:
    """people.A/B.match 可能是字符串或列表 → 统一成名字列表。"""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()] if v else []


def _preferred_mapping(candidates: list[dict]) -> dict:
    """给一份来源推荐 A/B 映射：**优先用 config 里已登记的「我 / 对方」账号名**。

    为什么不能直接用体检建议：`doctor.suggested_args` 是「消息较多的一方 = A」，
    纯统计猜测。对方话多时它会**把 A 选成对方**，而 A/B 选反会让这份来源的说话人
    整体颠倒（传导到关系五维、人格档案、推演语料），且多份来源一致选反时
    `map_conflicts` 抓不到。config.yaml 的 people.A/B.match 是用户自己的登记
    （支持列表，可登记多个来源的账号名），命中就优先用它。
    """
    names = [c.get("account") for c in (candidates or []) if c.get("account")]
    if len(names) < 2:
        return {}
    people = cfg_mod.load()["people"]
    a_hits = [n for n in names if n in _match_names(people.get("A", {}).get("match"))]
    b_hits = [n for n in names if n in _match_names(people.get("B", {}).get("match"))]
    if a_hits and b_hits and a_hits[0] != b_hits[0]:
        return {"sender_a": a_hits[0], "sender_b": b_hits[0], "source": "config"}
    return {}


def _attach_samples(src: Path, report: dict, candidates: list) -> None:
    """给每个候选账号附上前 3 条脱敏样例（只看 content_clean，不含原文 PII）。"""
    imp = (importer_registry.by_name(report["importer"])
           if report.get("importer") else None)
    if imp is None or not candidates:
        return
    samples: dict[str, list] = {}
    try:
        for rec in imp.parse(src):
            acct = rec.get("accountName") or "(空账号名)"
            bucket = samples.setdefault(acct, [])
            if len(bucket) < 3:
                text, _ = p1.mask_privacy(p1.clean_text(rec.get("content") or ""))
                bucket.append({"type": rec.get("type"),
                               "text": text[:200] or "[无文本内容]"})
    except Exception:
        return                              # 样例尽力而为，报告本身已含错误信息
    for c in candidates:
        c["samples"] = samples.get(c["account"], [])


@app.post("/api/import/merge")
def api_import_merge(body: ImportMergeBody):
    """按用户当前选定的逐源 A/B 重算合并预览（只读，不写库）。"""
    with _import_mutex:
        _bid, lock = _resolve_batch(body)
        sel = {s.import_id: ((s.sender_a or "").strip(), (s.sender_b or "").strip())
               for s in (body.sources or [])}
        specs = []
        skipped = []
        for f in _batch_files(lock, body.exclude):
            pair = sel.get(f["import_id"])
            if not pair or not pair[0] or not pair[1] or pair[0] == pair[1]:
                skipped.append(f["filename"])
                continue
            specs.append({"path": _import_file(f["import_id"]),
                          "sender_map": {pair[0]: "A", pair[1]: "B"},
                          "name": f["filename"], "importer": None})
        if not specs:
            raise HTTPException(400, "还没有任何一份来源选好了 A/B 双方账号")
        try:
            merge = p1.plan_merge(specs)
        except SystemExit as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"合并预览失败：{str(e)[:200]}")
        merge["provisional"] = False
        merge["skipped"] = skipped
        return {"merge": merge}


# ---------------------------------------------------------------- 提交
@app.post("/api/import/commit")
def api_import_commit(body: ImportCommitBody):
    """归档本批来源 → 从**全部已登记来源**重建库 → 关联媒体 → 报告。"""
    _require_no_analyze("导入记录")
    with _import_mutex:
        _bid, lock = _resolve_batch(body)
        files = _batch_files(lock, body.exclude)
        if not files:
            raise HTTPException(409, "这一批没有可导入的文件（都被移除了？），请重新上传")
        sel = _sel_map(lock, body)
        specs = _batch_specs(lock, sel, body.exclude)   # 逐源映射校验（缺 A/B 直接 400）
        tmp_ids = [f["import_id"] for f in files]
        try:
            # 1) 逐份体检 → 归档登记（体检不合格的不入库，直接报错让用户移除）
            for f, spec in zip(files, specs):
                src = spec["path"]
                report = run_doctor(src)
                if not report.get("recognized") or not (report.get("message_count") or 0):
                    raise HTTPException(
                        400, f"「{f['filename']}」解析不出消息"
                             f"（{report.get('verdict') or '格式未识别'}）——请把它移出本批")
                a, b = sel[f["import_id"]]
                first_ts, last_ts = _ts_pair(report.get("time_span"))
                import_sources.add_source(
                    src, f["filename"], report.get("importer") or "", {a: "A", b: "B"},
                    message_count=report.get("message_count") or 0,
                    first_ts=first_ts, last_ts=last_ts)
            # 2) 从全部已登记来源重建（含历史批次 → 自动按时间合并）
            all_specs, missing = import_sources.build_source_specs()
            if not all_specs:
                raise HTTPException(500, "来源登记为空，无法重建（请重新上传）")
            gap_minutes = body.session_gap_minutes or \
                int(cfg_mod.load()["chat"]["session_gap_minutes"])
            print(f"[i] 从 {len(all_specs)} 份已登记来源重建库"
                  f"（本批新增/更新 {len(files)} 份，映射 {sel}）")
            summary = p1.ingest_many(all_specs, db_path=cfg_mod.db_path(),
                                     session_gap_s=gap_minutes * 60)
            # 3) 媒体关联（有已导入媒体时才有意义；失败不阻塞导入）
            try:
                link = media_store.link_messages()
            except Exception as e:
                link = {"linked": 0, "candidates": 0, "unmatched": 0, "error": str(e)[:120]}
        except HTTPException:
            _import_cleanup(tmp_ids)
            raise
        except (SystemExit, Exception) as e:
            _import_cleanup(tmp_ids)
            raise HTTPException(500, f"导入失败: {str(e)[:300]}")
        finally:
            _import_cleanup(tmp_ids)
        _reset_caches()                              # 库被全量重建 → 引擎/检索器缓存全失效
        _schema_ready.clear()                        # 结构需重新确认
        summary["registered_sources"] = len(all_specs)
        summary["missing_sources"] = missing
        # 记住本次的「我 / 对方」账号名 → 下次导入界面按登记名预选，不再靠消息条数猜
        note = ""
        try:
            a_names = list(dict.fromkeys(v[0] for v in sel.values() if v[0]))
            b_names = list(dict.fromkeys(v[1] for v in sel.values() if v[1]))
            if cfg_mod.persist_sender_matches(a_names, b_names):
                note = "已把本次的「我 / 对方」账号名记进 config.yaml（下次导入会按它预选）"
        except Exception as e:
            note = f"「我 / 对方」账号名回填 config.yaml 失败（不影响导入）：{str(e)[:80]}"
        return {"ok": True, "summary": summary, "media_link": link,
                "config_note": note,
                "registered": import_sources.list_view()}


# ---------------------------------------------------------------- 已登记来源管理
@app.get("/api/import/sources")
def api_import_sources():
    return {"sources": import_sources.list_view(), "media": _media_stats_safe(),
            "spec_version": p1.SPEC_VERSION}


def _rebuild_from_sources() -> tuple[dict | None, list[str]]:
    """按当前全部已登记来源重建一次库（来源管理类操作共用）。

    返回 (summary, missing)；一份来源都不剩时清空库（persona 与媒体库保留）。
    调用方需自行持有 _import_mutex。
    """
    all_specs, missing = import_sources.build_source_specs()
    summary = None
    if all_specs:
        gap = int(cfg_mod.load()["chat"]["session_gap_minutes"])
        try:
            summary = p1.ingest_many(all_specs, db_path=cfg_mod.db_path(),
                                     session_gap_s=gap * 60)
        except (SystemExit, Exception) as e:
            raise HTTPException(500, f"重建失败: {str(e)[:300]}")
    else:
        try:
            cfg_mod.db_path().unlink()
        except OSError:
            pass
    _reset_caches()
    _schema_ready.clear()
    return summary, missing


class SourceMappingBody(BaseModel):
    """修正一份来源的 A/B 映射。默认动作是「对调」；也可显式给出 sender_a/sender_b。"""
    model_config = ConfigDict(extra="forbid")
    sender_a: str = ""
    sender_b: str = ""


@app.post("/api/import/sources/{source_id}/mapping")
def api_import_source_mapping(source_id: str, body: SourceMappingBody):
    """修正一份已导入来源的 A/B 映射，并立刻按全部来源重建库。

    为什么需要它：A/B 选反会让整份来源的说话人整体颠倒（关系状态、人格档案、
    推演语料全跟着错），而界面上最容易发生的就是「顺手用了默认值」。
    来源本来就有存档，所以这里**不需要重新上传**，改完直接重建（这正是留档的意义）。
    不传 sender_a/sender_b 时按「对调」处理 —— 选反是最常见的错误形态。
    """
    _require_no_analyze("修正来源映射")
    with _import_mutex:
        ent = import_sources.get(source_id)
        if ent is None:
            raise HTTPException(404, "该来源不在登记表里")
        smap = dict(ent.get("sender_map") or {})
        if len(smap) != 2:
            raise HTTPException(
                400, f"这份来源的映射不是 2 个账号（当前 {len(smap)} 个），无法自动对调；"
                     f"请把它移除后重新导入")
        names = list(smap.keys())
        a, b = (body.sender_a or "").strip(), (body.sender_b or "").strip()
        if a or b:
            if not a or not b or a == b:
                raise HTTPException(400, "需要给出两个不同的账号名")
            if {a, b} != set(names):
                raise HTTPException(
                    400, f"账号名必须是这份来源出现的两个账号：{'、'.join(names)}")
            new_map = {a: "A", b: "B"}
        else:
            # 对调：**按当前值取反**，而不是固定赋值 a→B / b→A。
            # 固定赋值会让「连点两次对调」静默失效（第二次等于重写同一个结果），
            # 用户看到的是「点了没反应」——这正是测试抓到的那个 bug。
            new_map = {n: ("A" if v == "B" else "B") for n, v in smap.items()}
        import_sources.set_sender_map(source_id, new_map)
        summary, missing = _rebuild_from_sources()
        return {"ok": True, "source_id": source_id,
                "sender_map": new_map,
                "summary": summary, "missing_sources": missing,
                "sources": import_sources.list_view()}


@app.delete("/api/import/sources/{source_id}")
def api_import_source_delete(source_id: str):
    """移除一份已登记来源（同时删其存档），并从剩余来源重建一次库。"""
    _require_no_analyze("移除来源")
    with _import_mutex:
        try:
            res = import_sources.remove_source(source_id)
        except KeyError:
            raise HTTPException(404, "该来源不在登记表里")
        summary, missing = _rebuild_from_sources()
        return {"ok": True, "removed": res.get("removed"), "summary": summary,
                "missing_sources": missing, "sources": import_sources.list_view()}


# ================================================================ 媒体导入通道（v0.3）
#
# 表情包 / 图片与聊天记录是两条独立通道：消息里只有文件名，真正的图片文件往往在
# 另一次导出里、或散落在用户自己收集的文件夹里。所以媒体必须能单独导入，
# 再靠「文件名精确匹配」与消息关联（message_media 表；匹配不上就留空，不猜）。
#
# 存储：data/profiles/<id>/media/<sha256 前16位><ext>（按内容哈希去重）
# ⚠️ 红线：媒体不做脱敏、不进 LLM —— 详见 docs/IMPORT.md「媒体导入」一节。
def _media_stats_safe() -> dict:
    try:
        return media_store.stats()
    except Exception as e:
        return {"count": 0, "by_kind": {}, "total_bytes": 0, "error": str(e)[:120]}


def _media_kind_param(v: str | None) -> str:
    k = (v or "auto").strip().lower()
    if k not in ("auto", "sticker", "image"):
        raise HTTPException(400, "kind 需为 auto / sticker / image")
    return k


@app.get("/api/media")
def api_media_library():
    """媒体库清单（含与消息的关联数、体积统计）。

    路径特意不用 `/api/media/library` —— 那会撞上 `/api/media/{name}`（同名单段路径
    先注册先匹配），"library" 会被当成文件名去查图片。
    """
    try:
        return {"items": media_store.list_media(), "stats": media_store.stats()}
    except Exception as e:
        raise HTTPException(500, f"读取媒体索引失败：{str(e)[:200]}")


@app.post("/api/media/import")
async def api_media_import(request: Request):
    """媒体独立导入：multipart 多文件（或裸 octet-stream）+ ?kind=auto|sticker|image。"""
    _require_no_analyze("导入媒体")
    raw = await request.body()
    kind = _media_kind_param(request.query_params.get("kind"))
    with _import_mutex:
        if len(raw) > MEDIA_REQUEST_MAX_BYTES:
            raise HTTPException(413, "本次上传体积过大，请分批导入")
        items = _extract_uploads(request, raw)
        if len(items) > MEDIA_MAX_FILES:
            raise HTTPException(400, f"单次最多 {MEDIA_MAX_FILES} 张")
        tmp = _media_tmp_dir()
        tmp.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        names: list[str] = []
        try:
            for data, filename in items:
                name = safe_filename(filename)
                if not data:
                    continue
                p = tmp / (uuid.uuid4().hex + (Path(name).suffix.lower() or ".bin"))
                p.write_bytes(data)
                paths.append(p)
                names.append(name)
            result = _media_import_pairing(paths, names, kind)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"媒体导入失败：{str(e)[:200]}")
        finally:
            for p in paths:
                try:
                    p.unlink()
                except OSError:
                    pass
        return result


class MediaDirBody(BaseModel):
    path: str
    kind: str = "auto"


@app.post("/api/media/import_dir")
def api_media_import_dir(body: MediaDirBody):
    """导入本机一个目录里的全部图片（典型用法：WeFlow 导出的 Emojis 文件夹）。

    只在本机 127.0.0.1 上提供；不移动原文件，只按内容哈希复制存档。
    """
    _require_no_analyze("导入媒体")
    kind = _media_kind_param(body.kind)
    d = Path((body.path or "").strip().strip('"').strip("'"))
    if not d:
        raise HTTPException(400, "请填写目录路径")
    if not d.is_dir():
        raise HTTPException(400, f"目录不存在或不可读：{d.name}")
    with _import_mutex:
        try:
            paths = sorted(p for p in d.iterdir()
                           if p.is_file() and p.suffix.lower() in media_store.ALLOWED_EXTS)
        except OSError as e:
            raise HTTPException(400, f"读取目录失败：{str(e)[:160]}")
        if not paths:
            raise HTTPException(400, "该目录下没有可导入的图片"
                                     "（支持 " + " / ".join(sorted(media_store.ALLOWED_EXTS)) + "）")
        if len(paths) > 5000:
            raise HTTPException(400, f"目录里图片过多（{len(paths)} 张），请分批导入")
        try:
            return _media_import_pairing(paths, [p.name for p in paths], kind)
        except Exception as e:
            raise HTTPException(500, f"媒体导入失败：{str(e)[:200]}")


def _media_import_pairing(paths: list[Path], names: list[str], kind: str) -> dict:
    """逐张归档 + 索引，然后与消息做一次关联。"""
    added, dedup, failed = [], [], []
    for p, name in zip(paths, names):
        r = media_store.import_file(p, kind=kind, original_name=name)
        if not r.get("ok"):
            failed.append({"name": r.get("filename") or p.name, "reason": r.get("reason")})
        elif r.get("dedup"):
            dedup.append(r["filename"])
        else:
            added.append(r["filename"])
    link = {"linked": 0, "candidates": 0, "unmatched": 0}
    try:
        link = media_store.link_messages()
    except Exception as e:
        link["error"] = str(e)[:120]
    return {"ok": True, "total": len(paths),
            "added": added, "dedup": dedup, "failed": failed,
            "added_count": len(added), "dedup_count": len(dedup),
            "failed_count": len(failed), "media_link": link,
            "stats": _media_stats_safe()}


@app.delete("/api/media/item/{media_id}")
def api_media_delete(media_id: str):
    _require_no_analyze("删除媒体")
    with _import_mutex:
        try:
            return {"ok": True, **media_store.delete(media_id),
                    "stats": _media_stats_safe()}
        except KeyError:
            raise HTTPException(404, "该媒体不在索引里")


# ================================================================ 分析任务（v0.3 R2）
#
# 生命周期：POST /api/analyze（登记 + 起后台线程）→ 前端轮询 /api/analyze/status
#          → 完成（或中止）后 last_result 保留到下次启动。
# 互斥：与导入向导、好友切换/删除共用 _op_lock；进行中重复触发一律 409。
class AnalyzeBody(BaseModel):
    skip_llm: bool = False
    max_sessions: int = 0


def _analyze_snapshot() -> dict:
    with _ANALYZE_LOCK:
        snap = dict(_ANALYZE)
    snap.pop("cancel", None)
    if snap["running"] and snap["started_at"]:
        snap["elapsed_s"] = round(time.time() - snap["started_at"], 1)
    snap["stages"] = [{"code": c, "label": lb} for c, lb in ANALYZE_STAGES]
    snap["profile"] = cfg_mod.active_profile()
    snap["profile_name"] = _profile_display_name()
    return snap


def _profile_display_name() -> str:
    pid = cfg_mod.active_profile()
    if not pid:
        return "当前数据目录"
    it = cfg_mod.profile_entry(pid)
    return (it or {}).get("name") or pid


def _analyze_worker(skip_llm: bool, max_sessions: int) -> None:
    import analyze_pipeline

    def progress(code: str, msg: str) -> None:
        with _ANALYZE_LOCK:
            _ANALYZE["phase"] = code
            _ANALYZE["message"] = msg
            _ANALYZE["elapsed_s"] = round(time.time() - _ANALYZE["started_at"], 1)

    def cancelled() -> bool:
        with _ANALYZE_LOCK:
            return bool(_ANALYZE["cancel"])

    result = None
    error = ""
    was_cancelled = False
    try:
        result = analyze_pipeline.run_all(skip_llm=skip_llm, max_sessions=max_sessions,
                                          progress=progress, should_cancel=cancelled)
    except analyze_pipeline.AnalyzeCancelled as e:
        was_cancelled, error = True, str(e)
    except SystemExit as e:                      # 库里没有消息等前置问题
        error = str(e) or "无法开始分析"
    except BaseException as e:                   # 兜底：后台异常绝不能让任务卡在 running
        error = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        _drop_all_engines()                      # 数据已变，对话引擎缓存全部失效
        with _ANALYZE_LOCK:
            _ANALYZE.update({
                "running": False,
                "finished_at": time.time(),
                "elapsed_s": round(time.time() - _ANALYZE["started_at"], 1),
                "error": "" if result is not None else error,
                "cancelled": was_cancelled,
                "message": ("分析完成" if result is not None
                            else ("已中止（产物可能不完整，可重新分析）" if was_cancelled else error)),
            })
            if result is not None:
                _ANALYZE["last_result"] = result


@app.post("/api/analyze")
def api_analyze(body: AnalyzeBody):
    """启动后台分析（与导入/好友切换互斥）"""
    with _op_lock:
        reason = _op_reason(exclude="analyze")
        if reason:
            raise HTTPException(409, f"{reason}，暂时不能开始分析")
        with _ANALYZE_LOCK:
            if _ANALYZE["running"]:
                raise HTTPException(409, "分析任务已在进行中")
            job_id = f"analyze-{int(time.time())}"
            _ANALYZE.update({
                "running": True, "phase": "1", "message": "正在启动分析…",
                "started_at": time.time(), "finished_at": 0.0, "elapsed_s": 0.0,
                "cancel": False, "cancelled": False, "error": "",
                "skip_llm": bool(body.skip_llm), "job_id": job_id,
                "profile": cfg_mod.active_profile(),
            })
    threading.Thread(target=_analyze_worker,
                     args=(bool(body.skip_llm), int(body.max_sessions or 0)),
                     name="ifwe-analyze", daemon=True).start()
    return {"job_id": job_id, "running": True, "skip_llm": bool(body.skip_llm)}


@app.get("/api/analyze/status")
def api_analyze_status():
    return _analyze_snapshot()


@app.post("/api/analyze/cancel")
def api_analyze_cancel():
    """请求中止（管线在每个阶段之间检查标志位）"""
    with _ANALYZE_LOCK:
        if not _ANALYZE["running"]:
            return {"ok": False, "message": "当前没有正在进行的分析"}
        _ANALYZE["cancel"] = True
        _ANALYZE["message"] = "正在中止…（当前阶段结束后停止）"
    return {"ok": True, "message": "已请求中止"}


# ================================================================ 人物档案（v0.3 R4）
#
# 只读展示：persona 四层（L/M/S/U）+ 关系五维当前值 + 转折点。
# 数据全部来自已有产物（persona JSON、relationship_state、turning_points），无新计算。
PERSONA_LAYER_META = {
    "L": {"label": "语言风格", "tone": "时不变", "hint": "用词习惯、句长、标点/表情、口头禅"},
    "M": {"label": "压力与意义", "tone": "中期", "hint": "近期在忙什么、压力源、在意什么"},
    "S": {"label": "情绪与冲突", "tone": "动态", "hint": "情绪起伏规律、冲突反应与修复方式"},
    "U": {"label": "自我认知", "tone": "用户校准真值", "hint": "本人认可的自我描述/底线"},
}


def _persona_file(person: str) -> Path:
    return cfg_mod.persona_dir() / f"persona_v1_{person}.json"


def _persona_read(person: str) -> dict:
    """读一份 persona JSON（缺失/损坏都不抛）"""
    p = _persona_file(person)
    names = cfg_mod.sender_names()
    out = {"person": person, "file": p.name, "has_file": p.is_file(),
           "display_name": "", "layers": {}, "empty": True}
    if not p.is_file():
        out["display_name"] = names.get(person, person)
        return out
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        out["display_name"] = names.get(person, person)
        return out
    if not isinstance(d, dict):
        out["display_name"] = names.get(person, person)
        return out
    layers: dict[str, list] = {}
    for lk in ("L", "M", "S", "U"):
        items = ((d.get("layers") or {}).get(lk) or {}).get("items") or []
        rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            text = (it.get("item") or "").strip()
            if text:
                rows.append({"label": (it.get("label") or "").strip(), "item": text})
        layers[lk] = rows
    out["layers"] = layers
    out["empty"] = not any(layers.values())
    out["display_name"] = (d.get("display_name") or "").strip() or names.get(person, person)
    return out


@app.get("/api/persona")
def api_persona():
    conn = _conn()
    rel = [{"period": p, "closeness": c, "conflict": cf, "trust": t,
            "emotional_safety": e, "comm_quality": q, "confidence": conf}
           for p, c, cf, t, e, q, conf in conn.execute(
               "SELECT period, closeness, conflict, trust, emotional_safety, comm_quality, "
               "confidence FROM relationship_state ORDER BY period")]
    tps = []
    for tp_id, day, rng, ttype, title, desc in conn.execute(
            "SELECT tp_id, day, date_range, tp_type, title, description FROM turning_points "
            "ORDER BY day"):
        meta = TP_TYPE_META.get(ttype, TP_TYPE_META["other"])
        tps.append({"id": tp_id, "day": day, "range": rng, "type": ttype,
                    "type_label": meta["label"], "tone": meta["tone"],
                    "title": title, "desc": desc})
    stats = {
        "n_messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "n_events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "n_facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        "rel_months": len(rel),
        "n_turning_points": len(tps),
    }
    span = conn.execute("SELECT MIN(day), MAX(day) FROM messages WHERE day IS NOT NULL").fetchone()
    stats["first_day"], stats["last_day"] = (span[0], span[1]) if span else (None, None)
    conn.close()

    people = {p: _persona_read(p) for p in ("A", "B")}
    pdir = cfg_mod.persona_dir()
    try:
        pdir_disp = pdir.relative_to(cfg_mod.root()).as_posix()
    except ValueError:
        pdir_disp = "（项目外目录）"
    return {
        "people": people,
        "layer_meta": PERSONA_LAYER_META,
        "rel": rel,
        "rel_current": rel[-1] if rel else None,
        "turning_points": tps,
        "stats": stats,
        "names": cfg_mod.sender_names(),
        "persona_dir": pdir_disp,
        "empty_templates": [p for p, v in people.items() if v["empty"]],
    }


# ================================================================ LLM 设置（v0.3 R3）
#
# 密钥红线：key 永不落 config.yaml；非空写入系统凭据管理器（见 secret_store）。
# GET /api/settings 任何响应都不含完整 key，只回 key_hint（如 sk-***abc）。
class SettingsBody(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    api_key: str | None = None          # None=不动；""=清除；非空=保存


class SettingsTestBody(BaseModel):
    base_url: str | None = None
    model: str | None = None


def _scrub_err(text: str, key: str = "") -> str:
    """错误信息脱敏：绝不把密钥片段带出服务端"""
    if key:
        text = text.replace(key, "***")
    text = re.sub(r"sk-[A-Za-z0-9_\-]{4,}", "sk-***", text)
    text = re.sub(r"(?i)(api[_-]?key\"?\s*[:=]\s*)[^\s,\"'}]+", r"\1***", text)
    return text


def _safe_text(value: str, field: str, maxlen: int) -> str:
    v = (value or "").strip()
    if len(v) > maxlen:
        raise HTTPException(400, f"{field} 过长（最多 {maxlen} 字符）")
    if any(c in v for c in "\r\n\t"):
        raise HTTPException(400, f"{field} 不能包含换行/制表符")
    return v


def _settings_view() -> dict:
    cfg = cfg_mod.load()
    llm = cfg["llm"]
    key = cfg_mod.api_key()
    desc = secret_store.describe()
    return {
        "provider": llm.get("provider", "deepseek"),
        "base_url": llm.get("base_url", ""),
        "model": llm.get("model", ""),
        "api_key_env": llm.get("api_key_env", "LLM_API_KEY"),
        "max_tokens": llm.get("max_tokens", 8192),
        "key_state": "set" if key else "unset",
        "key_hint": secret_store.hint(key) if key else "",
        "key_backend": desc.get("backend"),
        "keyring_available": desc.get("keyring_available"),
        "key_from_env": bool(os.environ.get(llm.get("api_key_env") or "LLM_API_KEY")),
        "allow_llm_send": bool(cfg["privacy"]["allow_llm_send"]),
        "config_file": "config.yaml" if cfg_mod.settings_path().is_file() else "",
        "providers": [{"value": "deepseek", "label": "DeepSeek"},
                      {"value": "custom", "label": "自定义（任意 OpenAI 兼容端点）"}],
    }


@app.get("/api/settings")
def api_settings():
    return _settings_view()


@app.put("/api/settings")
def api_settings_put(body: SettingsBody):
    updates: dict = {}
    if body.provider is not None:
        updates["provider"] = _safe_text(body.provider, "provider", 32) or "deepseek"
    if body.base_url is not None:
        v = _safe_text(body.base_url, "base_url", 200)
        if v and not re.match(r"^https?://", v):
            raise HTTPException(400, "base_url 需以 http:// 或 https:// 开头")
        updates["base_url"] = v
    if body.model is not None:
        updates["model"] = _safe_text(body.model, "model", 64)
    if body.max_tokens is not None:
        try:
            n = int(body.max_tokens)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_tokens 需为整数")
        if not 1 <= n <= 32768:
            raise HTTPException(400, "max_tokens 需在 1~32768 之间")
        updates["max_tokens"] = n

    env_name = cfg_mod.load()["llm"]["api_key_env"] or "LLM_API_KEY"
    notes: list[str] = []
    if body.api_key is not None:
        k = (body.api_key or "").strip()
        if not k:                                        # 空串 = 清除
            had = secret_store.clear_key(env_name)
            os.environ.pop(env_name, None)
            notes.append("已清除 API Key" if had else "本就没有保存过 API Key")
        else:
            if len(k) > 4096:
                raise HTTPException(400, "密钥过长")
            backend = secret_store.save_key(k, env_name)
            os.environ[env_name] = k                     # 立即生效，免重启
            notes.append("密钥已保存到" + ("Windows 凭据管理器" if backend == "credential"
                                          else "DPAPI 加密文件（当前用户）"))
    if updates:
        written, note = cfg_mod.persist_settings(updates)
        if "失败" in note or ("无法" in note and not written):
            raise HTTPException(500, note)
        notes.append(note)

    phase2_llm.refresh_endpoint()                        # base_url/model 立即生效
    _reset_caches()                                      # 配置变更 → 引擎缓存全失效
    view = _settings_view()
    view["message"] = "；".join(n for n in notes if n) or "没有需要修改的字段"
    return view


@app.post("/api/settings/test")
def api_settings_test(body: SettingsTestBody | None = None):
    """用当前配置发一次最小 chat 补全（max_tokens=1，超时 10s）验证连通性"""
    t0 = time.time()
    llm = cfg_mod.load()["llm"]
    base_url = ((body.base_url or "").strip() if body else "") or \
        (os.environ.get("IFWE_LLM_BASE_URL") or str(llm.get("base_url") or ""))
    model = ((body.model or "").strip() if body else "") or \
        (os.environ.get("IFWE_LLM_MODEL") or str(llm.get("model") or ""))
    key = cfg_mod.api_key()
    latency = lambda: int((time.time() - t0) * 1000)      # noqa: E731
    if not key:
        return {"ok": False, "model": model, "base_url": base_url, "latency_ms": latency(),
                "message": "还没有可用的 API Key：请先填写并保存密钥"}
    try:
        from openai import OpenAI
        cli = OpenAI(api_key=key, base_url=base_url, timeout=10.0, max_retries=0)
        resp = cli.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "ping"}],
            max_tokens=1, temperature=0)
        used = getattr(resp, "model", "") or model
        return {"ok": True, "model": model, "used_model": used, "base_url": base_url,
                "latency_ms": latency(), "message": f"连接成功（{used}）"}
    except Exception as e:
        return {"ok": False, "model": model, "base_url": base_url, "latency_ms": latency(),
                "message": _scrub_err(str(e), key)[:300] or "连接失败"}


# ================================================================ 多好友 profile（v0.3 R5）
#
# 每个好友 = 一个独立数据目录（库/persona/表情包/对话线全隔离），
# 切换 = set_active_profile + 清空引擎与检索器缓存；进行中禁止切换/删除。
class ProfileNewBody(BaseModel):
    name: str = ""
    id: str | None = None


class ProfileSwitchBody(BaseModel):
    id: str


class ProfileNameBody(BaseModel):
    name: str


def _profiles_view() -> dict:
    st = profiles_mod.status()
    st["busy"] = _op_reason()
    return st


@app.get("/api/profiles")
def api_profiles():
    return _profiles_view()


@app.post("/api/profiles")
def api_profiles_create(body: ProfileNewBody):
    with _op_lock:
        _require_idle("新建好友")
        try:
            entry = profiles_mod.create(body.name, body.id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "profile": entry, **_profiles_view()}


@app.post("/api/profiles/switch")
def api_profiles_switch(body: ProfileSwitchBody):
    with _op_lock:
        _require_idle("切换好友")
        pid = (body.id or "").strip()
        if pid and not cfg_mod.profile_entry(pid):
            if pid == profiles_mod.DEMO_ID:
                profiles_mod.ensure_demo_profile()
            else:
                raise HTTPException(404, "好友不存在")
        cfg_mod.set_active_profile(pid)
        _reset_caches()
        if pid:
            profiles_mod.touch(pid)
        return {"ok": True, "active": pid, **_profiles_view()}


@app.delete("/api/profiles/{pid}")
def api_profiles_delete(pid: str):
    with _op_lock:
        _require_idle("删除好友")
        if not cfg_mod.profile_entry(pid):
            raise HTTPException(404, "好友不存在")
        was_active = cfg_mod.active_profile() == pid
        try:
            res = profiles_mod.delete(pid)
        except KeyError:
            raise HTTPException(404, "好友不存在")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not res.get("ok"):
            raise HTTPException(500, "删除未完成（部分文件被占用，可重试）："
                                     + "；".join(res.get("errors") or []))
        if was_active:
            rest = cfg_mod.read_registry()
            cfg_mod.set_active_profile(rest[0]["id"] if rest else "")
            _reset_caches()
        return {"ok": True, "removed": res.get("removed", ""), **_profiles_view()}


@app.post("/api/profiles/{pid}/rename")
def api_profiles_rename(pid: str, body: ProfileNameBody):
    with _op_lock:
        _require_idle("重命名好友")
        try:
            entry = profiles_mod.rename(pid, body.name)
        except KeyError:
            raise HTTPException(404, "好友不存在")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "profile": entry, **_profiles_view()}


# ================================================================ 首次运行引导（v0.3 R6）
def _db_message_count(db: Path) -> int:
    if not db.is_file():
        return 0
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return int(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] or 0)
        finally:
            c.close()
    except Exception:
        return 0


def _first_run_state() -> dict:
    has_config = cfg_mod.settings_path().is_file()
    items = cfg_mod.read_registry()
    n_msgs = 0
    demo_ready = False
    for it in items:
        d = cfg_mod.profile_data_dir(it.get("id"))
        if d is None:
            continue
        cnt = _db_message_count(d / "ifwe_v1.db")
        n_msgs += cnt
        if it.get("id") == profiles_mod.DEMO_ID and cnt > 0:
            demo_ready = True
    if not items and not n_msgs:
        n_msgs = _db_message_count(cfg_mod.data_dir() / "ifwe_v1.db")
    key = cfg_mod.api_key()
    return {"fresh": (not has_config) and n_msgs == 0,
            "has_config": has_config, "has_messages": n_msgs > 0,
            "n_messages": n_msgs, "n_profiles": len(items),
            "profile": cfg_mod.active_profile(),
            "profile_name": _profile_display_name(),
            "key_state": "set" if key else "unset",
            "demo_ready": demo_ready}


@app.get("/api/onboarding")
def api_onboarding():
    return _first_run_state()


@app.post("/api/onboarding/demo")
def api_onboarding_demo():
    """体验示例数据：构建「示例好友（虚构数据）」并切过去（等价 run.py demo 的数据部分）"""
    with _op_lock:
        _require_idle("构建示例数据")
        entry = profiles_mod.ensure_demo_profile()
        prev = cfg_mod.active_profile()
        cfg_mod.set_active_profile(profiles_mod.DEMO_ID)
        try:
            d = cfg_mod.data_dir()
            built = False
            if _db_message_count(d / "ifwe_v1.db") == 0:
                _reset_caches()
                summary = profiles_mod.build_demo_library()
                built = True
            else:
                summary = {"message_count": _db_message_count(d / "ifwe_v1.db")}
            _reset_caches()
            profiles_mod.touch(profiles_mod.DEMO_ID)
        except Exception as e:
            cfg_mod.set_active_profile(prev)
            _reset_caches()
            raise HTTPException(500, f"示例数据构建失败：{_scrub_err(str(e))[:200]}")
    return {"ok": True, "built": built, "summary": summary,
            "profile": entry, **_profiles_view()}


@app.on_event("startup")
def _on_startup() -> None:
    """启动：老数据迁移到默认好友 + 恢复上次选择的好友"""
    try:
        mig = profiles_mod.ensure_migrated()
        if not mig.get("ok"):
            print(f"[profiles] 迁移未完成：{mig.get('error')}", flush=True)
        elif mig.get("migrated"):
            print(f"[profiles] 已把 data/ 迁移到 data/profiles/default/"
                  f"（备份 {mig.get('backup')}）", flush=True)
    except Exception as e:
        print(f"[profiles] 迁移异常（已忽略，原数据未改动）：{e}", flush=True)
    try:
        items = cfg_mod.read_registry()
        env_pid = os.environ.get(cfg_mod.ACTIVE_PROFILE_ENV, "")
        # 优先：环境变量 > 启动前已显式设置的好友（run.py server --profile / _use_profile）
        #       > 上次使用的好友 > 注册表第一项
        pid = env_pid or cfg_mod.active_profile()
        if not pid and items:
            ranked = sorted(items, key=lambda it: it.get("last_active") or "", reverse=True)
            pid = ranked[0].get("id") or ""
        if pid:
            cfg_mod.set_active_profile(pid)
            pc.refresh_paths()
    except Exception as e:
        print(f"[profiles] 恢复当前好友失败（已忽略）：{e}", flush=True)


# ---------------------------------------------------------------- 静态前端
app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    print(f"IfWe 对话服务启动: http://127.0.0.1:{PORT}", flush=True)
    if not cfg_mod.api_key():
        print(f"[warn] 未检测到 LLM API Key（环境变量 {cfg_mod.load()['llm']['api_key_env']}"
              f" 或界面「设置」面板）—— 可以浏览与离线分析，但发消息会报错", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
