# -*- coding: utf-8 -*-
"""Phase 17 · 决策观测（二期方案 v2 §11 Phase A：观测与路径一致性，不改用户体验）

只观测、不决策：本模块不参与「是否回复」的判定，只把产品/回放路径每轮的
决策过程落一行到 `sim_decision_log`（加法迁移，不碰既有表），让二期后续阶段
（Phase B 锚定 / Phase C 立场）有可比对的观测基线，并满足 Phase A 两条验收：

  1. 「同一切点用实验路径与产品路径时能解释差异」——每轮记录
     p_real_only（仅真实历史的对照 P）与 n_branch（样本中来自分支段的条数），
     产品路径 P 与实验路径 P 的差可逐轮归因；
  2. 「所有沉默与回退都有原因」——silent_reason 必填：沉默轮记
     not_replied;{why}，闸门异常记 decision_error;...，开关关闭记 gate_skipped;...

字段分两批：
  - 本阶段就有值：path / decided / p_reply / layer / samples / reply_mode /
    silent_reason / prompt_digest / state_before / state_after / source /
    n_branch / p_real_only / sfb_flag / anchor_day（Phase B 起）
  - 预留（后续阶段落值，当前恒 NULL）：m（锚定已落值，Phase C 起才有非 1.0）、
    stance_version / evidence_ids（Phase C）

自反馈检测（§5.1；Phase B 锚定后产品路径分支模式不再含分支样本）：
    sfb_flag = 1 ⇔ 当前 P 用到了分支段的 day 粒度近似标签、且与仅真实历史的
    P 不一致——即「模拟 B 的输出正在影响标签」。Phase B 锚定（裁定三）落地后
    产品路径恒为 0；legacy 回滚开关下仍可能为 1（保留检测能力以便审计）。

隐私：prompt_digest 只存 sha256 前 16 位与各组件长度，绝不落 prompt 原文。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import phase5_common as pc

# 与 phase5_schema.sql 尾部定义保持一致（ensure_schema 供旧库/内存副本就地补表，
# 属加法迁移：CREATE IF NOT EXISTS，不改动任何既有表）
DDL = """
CREATE TABLE IF NOT EXISTS sim_decision_log (
    log_id         TEXT PRIMARY KEY,
    sim_id         TEXT NOT NULL,
    session_id     TEXT,
    day            TEXT,
    turn_idx       INTEGER,
    path           TEXT NOT NULL,
    decided        INTEGER NOT NULL,
    p_reply        REAL,
    layer          TEXT,
    samples        INTEGER,
    n_branch       INTEGER,
    p_real_only    REAL,
    sfb_flag       INTEGER DEFAULT 0,
    anchor_day     TEXT,
    m              REAL,
    reply_mode     TEXT,
    silent_reason  TEXT,
    stance_version TEXT,
    evidence_ids   TEXT,
    prompt_digest  TEXT,
    state_before   TEXT,
    state_after    TEXT,
    source         TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simdec_sim ON sim_decision_log(sim_id, day, turn_idx);
"""

PATH_PRODUCT = "product"
PATH_REPLAY = "replay"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """就地补建 sim_decision_log（幂等；旧库与新库都安全）"""
    conn.executescript(DDL)


def fidelity_source() -> str:
    """当前保真实现来源（ported / legacy / off），仅作观测标注。

    两树通用：私有树有 phase16_persona_fix 垫片（Phase 0 起）→ 按其开关返回
    ported/legacy；发布树直连 persona_fidelity（无垫片）→ 返回 ported。
    """
    try:
        import phase16_persona_fix as shim      # 私有树：垫片开关
        return shim._source()
    except Exception:
        pass
    try:
        import persona_fidelity                  # 发布树：直连路线
        return "ported" if hasattr(persona_fidelity, "estimate_reply_probability") \
            else "off"
    except Exception:
        return "off"


def prompt_digest(parts: dict) -> str:
    """装配摘要：sha256 前 16 位 + 各组件长度（JSON）。不含任何原文。"""
    blob = "\x1e".join(str(v) for v in parts.values())
    h = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    lens = {k: len(str(v)) for k, v in parts.items()}
    return json.dumps({"sha": h, **lens}, ensure_ascii=False, sort_keys=True)


def _branch_start(conn: sqlite3.Connection, sim_id: str) -> str | None:
    if not sim_id:
        return None
    row = conn.execute("SELECT start_day FROM sim_runs WHERE sim_id=?",
                       (sim_id,)).fetchone()
    return row[0] if row else None


def snapshot_estimate(conn: sqlite3.Connection, sim_id: str, day: str) -> dict | None:
    """决策时刻的意愿估计快照 + 自反馈检测。

    重算一次（不掷骰、不改变决策）：
      est         = persona_fidelity.estimate_reply_probability(sim_id=..., ...)
      p_real_only = 同参数但 sim_id=""（只用分支起点前的真实历史）
      sfb_flag    = 1 ⇔ est 样本含分支段（n_branch>0）且 p 与 p_real_only 有差
    非 ported 来源（legacy / off）或估计失败 → None（观测缺失，不影响决策）。
    """
    if fidelity_source() != "ported":
        return None
    try:
        import persona_fidelity as pf
        bs = _branch_start(conn, sim_id)
        est = dict(pf.estimate_reply_probability(
            conn, sim_id=sim_id or "", start_day=bs or day, current_day=day))
        real = pf.estimate_reply_probability(
            conn, sim_id="", start_day=bs or day, current_day=day)
        n_branch = int(est.get("n_branch") or 0)
        flagged = (n_branch > 0
                   and abs(float(est["p"]) - float(real["p"])) > 1e-9)
        return {"est": est, "p_real_only": real["p"], "n_branch": n_branch,
                "sfb_flag": 1 if flagged else 0}
    except Exception:
        return None


def log_decision(conn: sqlite3.Connection, *, sim_id: str, session_id: str,
                 day: str, turn_idx: int, path: str, decided: int,
                 snapshot: dict | None = None, p_reply: float | None = None,
                 reply_mode: str | None = None, silent_reason: str | None = None,
                 prompt_digest: str | None = None, state_before: dict | None = None,
                 state_after: dict | None = None, source: str | None = None) -> None:
    """落一行决策观测（沉默轮与回复轮都落；INSERT OR REPLACE 幂等）。

    p_reply 以调用方传入为准（should_reply 实际用到的值）；
    layer/samples/n_branch/p_real_only/sfb_flag 取自 snapshot（估计快照）。
    """
    ensure_schema(conn)
    snap = snapshot or {}
    est = snap.get("est") or {}
    conn.execute(
        "INSERT OR REPLACE INTO sim_decision_log "
        "(log_id, sim_id, session_id, day, turn_idx, path, decided, p_reply, layer, "
        " samples, n_branch, p_real_only, sfb_flag, anchor_day, m, reply_mode, "
        " silent_reason, stance_version, evidence_ids, prompt_digest, state_before, "
        " state_after, source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{session_id}-D{int(turn_idx):03d}", sim_id, session_id, day,
         int(turn_idx), path, int(decided),
         p_reply, est.get("layer"), est.get("samples"),
         snap.get("n_branch"), snap.get("p_real_only"), snap.get("sfb_flag"),
         est.get("anchor_day"), est.get("m"),     # anchor_day（Phase B）/ m（裁定三，初版恒 1.0）
         reply_mode, silent_reason,
         None, None,                      # stance_version / evidence_ids（Phase C）
         prompt_digest,
         json.dumps(state_before or {}, ensure_ascii=False),
         json.dumps(state_after or {}, ensure_ascii=False),
         source, pc.now_str()))
    conn.commit()


def read_decisions(conn: sqlite3.Connection, sim_id: str,
                   limit: int = 0) -> list[dict]:
    """按落库顺序读一条线的决策观测（limit>0 时取最近 limit 行）"""
    sql = ("SELECT log_id, sim_id, session_id, day, turn_idx, path, decided, p_reply, "
           "layer, samples, n_branch, p_real_only, sfb_flag, anchor_day, m, reply_mode, "
           "silent_reason, stance_version, evidence_ids, prompt_digest, state_before, "
           "state_after, source, created_at FROM sim_decision_log WHERE sim_id=? "
           "ORDER BY rowid")
    cols = ["log_id", "sim_id", "session_id", "day", "turn_idx", "path", "decided",
            "p_reply", "layer", "samples", "n_branch", "p_real_only", "sfb_flag",
            "anchor_day", "m", "reply_mode", "silent_reason", "stance_version",
            "evidence_ids", "prompt_digest", "state_before", "state_after",
            "source", "created_at"]
    rows = conn.execute(sql, (sim_id,)).fetchall()
    if limit > 0:
        rows = rows[-limit:]
    return [dict(zip(cols, r)) for r in rows]


def last_decision(conn: sqlite3.Connection, sim_id: str) -> dict | None:
    ds = read_decisions(conn, sim_id, limit=1)
    return ds[-1] if ds else None
