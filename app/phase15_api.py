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
from pydantic import BaseModel

import config as cfg_mod
import phase1_ingest as p1
import phase2_llm
import phase4_retrieval
import phase5_common as pc
import profiles as profiles_mod
import secret_store
from doctor import run_doctor
from importers import registry as importer_registry
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


# ================================================================ Web 导入向导（v0.2 O-5f）
#
# 生命周期：upload（落临时文件 + 取 .lock）→ preview（体检+A/B 候选+脱敏样例）
#           → commit（完整 ingest+门禁）→ **链路结束（含异常路径）立即删除临时文件与锁**。
# 安全：仅本机 127.0.0.1；响应不含服务器路径；预览只回统计 + 每方前 3 条脱敏样例；
#       白名单扩展名 + ≤50MB + import_id 严格 hex 校验 + 同时仅一个导入任务。
IMPORT_MAX_BYTES = 50 * 1024 * 1024
IMPORT_ALLOWED_EXTS = {".jsonl", ".json", ".csv"}
IMPORT_STALE_S = 3600                                    # 锁/临时文件过期回收阈值
_import_mutex = _op_lock                                 # 与分析/好友切换共用同一把互斥锁


def _import_tmp_dir() -> Path:
    """导入临时目录（随当前好友的数据目录走；data/ 已 gitignore，绝不入库）"""
    return cfg_mod.data_dir() / "tmp_import"


class ImportPreviewBody(BaseModel):
    import_id: str


class ImportCommitBody(BaseModel):
    import_id: str
    sender_a: str
    sender_b: str
    session_gap_minutes: int | None = None


def _import_file(import_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", import_id or ""):
        raise HTTPException(400, "非法 import_id")
    tmp_dir = _import_tmp_dir()
    if not tmp_dir.is_dir():
        raise HTTPException(404, "临时文件不存在（已过期或已完成）")
    files = sorted(tmp_dir.glob(import_id + ".*"))
    if not files:
        raise HTTPException(404, "临时文件不存在（已过期或已完成）")
    return files[0]


def _lock_read() -> dict | None:
    try:
        return json.loads((_import_tmp_dir() / ".lock").read_text(encoding="utf-8"))
    except Exception:
        return None


def _lock_write(meta: dict) -> None:
    (_import_tmp_dir() / ".lock").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _import_cleanup(import_id: str | None = None) -> None:
    """逐文件 unlink（本机 safe-delete 对目录级删除 fail-closed，勿整目录删）。"""
    tmp_dir = _import_tmp_dir()
    try:
        (tmp_dir / ".lock").unlink()
    except OSError:
        pass
    if import_id and tmp_dir.is_dir():
        for p in tmp_dir.glob(import_id + ".*"):
            try:
                p.unlink()
            except OSError:
                pass


def _purge_stale_import() -> None:
    """回收超时锁与孤儿临时文件（客户端中途放弃的兜底）。"""
    tmp_dir = _import_tmp_dir()
    lockp = tmp_dir / ".lock"
    if lockp.exists():
        age = time.time() - lockp.stat().st_mtime
        if age > IMPORT_STALE_S:
            _import_cleanup()
    if tmp_dir.is_dir():
        for p in tmp_dir.iterdir():
            if p.name == ".lock":
                continue
            if time.time() - p.stat().st_mtime > IMPORT_STALE_S:
                try:
                    p.unlink()
                except OSError:
                    pass


def _extract_upload(request: Request, body: bytes) -> tuple[bytes, str]:
    """支持 multipart/form-data（手写解析，零新增依赖）与裸 octet-stream 两种上传。"""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            raise HTTPException(400, "multipart 缺少 boundary")
        sep = b"--" + m.group(1).encode()
        for seg in body.split(sep)[1:]:
            if seg.strip(b"\r\n-") == b"":
                continue
            if b"\r\n\r\n" in seg:
                head, _, payload = seg.partition(b"\r\n\r\n")
                headers = head.decode("utf-8", "replace")
                if 'filename="' in headers:
                    fm = re.search(r'filename="([^"]*)"', headers)
                    if payload.endswith(b"\r\n"):
                        payload = payload[:-2]
                    return payload, (fm.group(1) if fm else "")
        raise HTTPException(400, "multipart 中未找到文件字段")
    return body, request.query_params.get("filename", "")


@app.post("/api/import/upload")
async def api_import_upload(request: Request):
    _require_no_analyze("导入记录")
    raw = await request.body()
    with _import_mutex:
        _purge_stale_import()
        if _lock_read() is not None:
            raise HTTPException(409, "已有导入任务进行中，请稍后再试")
        data, filename = _extract_upload(request, raw)
        if not data:
            raise HTTPException(400, "上传内容为空")
        if len(data) > IMPORT_MAX_BYTES:
            raise HTTPException(413, "文件超过 50MB 上限")
        ext = Path(filename or "").suffix.lower()
        if ext not in IMPORT_ALLOWED_EXTS:
            raise HTTPException(400, "仅支持 .jsonl / .json / .csv 文件")
        tmp_dir = _import_tmp_dir()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        import_id = uuid.uuid4().hex
        tmp = tmp_dir / (import_id + ext)
        try:
            tmp.write_bytes(data)
            _lock_write({"import_id": import_id, "filename": Path(filename).name,
                         "created": time.time()})
        except OSError as e:
            _import_cleanup(import_id)
            raise HTTPException(500, f"落盘失败: {e}")
        return {"import_id": import_id, "filename": Path(filename).name,
                "size": len(data)}


@app.post("/api/import/preview")
def api_import_preview(body: ImportPreviewBody):
    with _import_mutex:
        if not _lock_read() or _lock_read().get("import_id") != body.import_id:
            raise HTTPException(409, "导入会话不存在或已失效，请重新上传")
        src = _import_file(body.import_id)
        try:
            report = run_doctor(src)
            candidates = report.get("candidates") or []
            # 每方前 3 条脱敏样例（content_clean，不含原文 PII）
            samples: dict[str, list] = {}
            imp = (importer_registry.by_name(report["importer"])
                   if report.get("importer") else None)
            if imp is not None:
                try:
                    for rec in imp.parse(src):
                        acct = rec.get("accountName") or "(空账号名)"
                        bucket = samples.setdefault(acct, [])
                        if len(bucket) < 3:
                            text, _ = p1.mask_privacy(p1.clean_text(rec.get("content") or ""))
                            bucket.append({"type": rec.get("type"),
                                           "text": text[:200] or "[无文本内容]"})
                except Exception:
                    pass                    # 样例尽力而为，报告本身已含错误信息
            for c in candidates:
                c["samples"] = samples.get(c["account"], [])
        except Exception as e:
            _import_cleanup(body.import_id)          # 预览失败即清理
            raise HTTPException(500, f"预览失败: {str(e)[:200]}")
        if not report.get("recognized"):
            _import_cleanup(body.import_id)          # 无法识别的文件直接清理
        return {"import_id": body.import_id, "report": report,
                "candidates": candidates}


@app.post("/api/import/commit")
def api_import_commit(body: ImportCommitBody):
    _require_no_analyze("导入记录")
    sender_a = (body.sender_a or "").strip()
    sender_b = (body.sender_b or "").strip()
    if not sender_a or not sender_b:
        raise HTTPException(400, "需要选择 A/B 双方账号")
    if sender_a == sender_b:
        raise HTTPException(400, "A/B 不能是同一个账号")
    with _import_mutex:
        lock = _lock_read()
        if not lock or lock.get("import_id") != body.import_id:
            raise HTTPException(409, "导入会话不存在或已失效，请重新上传")
        src = _import_file(body.import_id)
        gap_minutes = body.session_gap_minutes or \
            int(cfg_mod.load()["chat"]["session_gap_minutes"])
        try:
            summary = p1.ingest(src, {sender_a: "A", sender_b: "B"},
                                db_path=cfg_mod.db_path(),
                                session_gap_s=gap_minutes * 60)
        except (SystemExit, Exception) as e:
            raise HTTPException(500, f"导入失败: {str(e)[:300]}")
        finally:
            _import_cleanup(body.import_id)          # 链路结束（含失败）立即清理
        for sid in list(_engines):                   # 旧库已被重建，缓存引擎全部失效
            _drop_engine(sid)
        _schema_ready.clear()                        # 库被全量重建，结构需重新确认
        return {"ok": True, "summary": summary}


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
