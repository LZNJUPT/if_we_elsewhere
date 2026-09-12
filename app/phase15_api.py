# -*- coding: utf-8 -*-
"""
IfWe Phase 15 · 对话产品服务层（M2 + M3）

把对话内核包成一个「对话推演」的本地 Web 服务：
  M3 聊天窗口 —— 打开就是对话框，不是仪表盘
  M2 任意节点切入 —— 在时间轴上选一天，写一句改写，开一条 IF 线

设计原则（沿用项目约定）:
  - 只读主库；一切对话写入 sim_* 隔离命名空间（DialEngine 保证）
  - 不公开部署：仅监听 127.0.0.1
  - 密钥只从环境变量读，不写文件
  - 零构建、无 CDN：原生 HTML/CSS/JS

启动:
  python run.py server          # 项目根执行 → http://127.0.0.1:8015
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config as cfg_mod
import phase4_retrieval
import phase5_common as pc
from phase15_dial_engine import (AUTO_DAY_TURNS, get_default_start, DialEngine,
                                 _dial_lines, new_line)
from phase5_llm import get_client

WEB_DIR = Path(__file__).resolve().parent / "phase15_web"
PORT = int(os.environ.get("PORT") or cfg_mod.load()["defaults"]["port"])

# 真实表情包文件目录（如 WeFlow 导出的 Emojis 文件夹）；路径在 config media.emojis_dir 配置，
# 可用环境变量 IFWE_MEDIA_DIR 覆盖。未配置时 /api/media 返回 404，界面自动降级为占位符。
MEDIA_NAME = re.compile(r"^[0-9a-f]{32}\.(gif|jpg|jpeg|png)$", re.I)
MEDIA_MIME = {".gif": "image/gif", ".png": "image/png",
              ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

app = FastAPI(title="IfWe · 对话推演", version="0.1.0")

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


SCHEMA_IF = Path(__file__).resolve().parent / "schema_if.sql"


def _conn() -> sqlite3.Connection:
    conn = pc.connect()
    pc.apply_schema(conn)
    conn.executescript(SCHEMA_IF.read_text(encoding="utf-8"))   # ifr_branch（分支元数据）
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


# ---------------------------------------------------------------- API
@app.get("/api/health")
def api_health():
    key_env = cfg_mod.load()["llm"]["api_key_env"]
    return {"ok": True, "key": bool(cfg_mod.api_key()), "key_env": key_env,
            "port": PORT}


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
    """表情包文件（仅允许 32 位 hex + 图片扩展名，杜绝路径穿越）"""
    if not MEDIA_NAME.match(name):
        raise HTTPException(400, "非法文件名")
    base = cfg_mod.emojis_dir()
    if base is None:
        raise HTTPException(404, "未配置表情包目录（config.yaml media.emojis_dir）")
    p = base / name
    try:
        if not p.is_file():
            raise HTTPException(404, "文件不存在")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "文件不可读")
    return FileResponse(str(p), media_type=_sniff_mime(p))


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
    if not cfg_mod.api_key():
        key_env = cfg_mod.load()["llm"]["api_key_env"]
        raise HTTPException(400, f"缺少 LLM API Key：请设置环境变量 {key_env} 后重启服务")
    try:
        eng = _get_engine(body.line)
        res = eng.say(text)
    except Exception as e:
        _drop_engine(body.line)
        raise HTTPException(500, f"生成失败: {str(e)[:300]}")
    return {"day": res["day"], "reply": res["reply"], "medium": res["medium"],
            "action": res["action"], "sticker": res.get("sticker"),
            "event_note": res.get("event_note"),
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


# ---------------------------------------------------------------- 静态前端
app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    print(f"IfWe 对话服务启动: http://127.0.0.1:{PORT}", flush=True)
    if not cfg_mod.api_key():
        print(f"[warn] 未检测到 LLM API Key（环境变量 {cfg_mod.load()['llm']['api_key_env']}）"
              "—— 可以浏览界面，但发消息会报错", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
