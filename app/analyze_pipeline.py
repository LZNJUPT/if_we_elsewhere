# -*- coding: utf-8 -*-
"""
IfWe · 分析流水线 v0.1（run.py analyze 的实现）

从已导入的规范库生成五类产物（全部只依赖你自己的数据，写入独立表/文件）：
  1. events               —— 会话级事件抽取（LLM 或关键词启发式）
  2. facts                —— 记忆（episodic 事件派生；检索用）
  3. relationship_state   —— 逐月五维关系状态（简化估计量，非真值）
  4. turning_points       —— 转折点/岔路口标记（断联窗口 + 重要事件 + 活跃高峰）
  5. persona JSON         —— 双人 L/M/S/U 人格档案（LLM 生成或手写模板）

两条路径:
  LLM 路径（默认）: 会话事件抽取 + 人格档案生成，需要 API Key（config llm.api_key_env）
  离线路径 --skip-llm: 关键词启发式事件 + persona 模板（占位，可手写后重跑）

v0.1 是简化版分析器（完整版在路线图）：够跑通主线，数字口径以「估计量」为准。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

import config as cfg_mod
import phase5_common as pc
import phase6_engine as p6

APP_DIR = cfg_mod.resource_dir()        # schema 所在（源码=app/；PyInstaller=_MEIPASS/app）
SCHEMAS = ["schema_v1.sql", "phase2_schema.sql", "phase4_schema.sql",
           "phase5_schema.sql", "phase6_schema.sql", "schema_if.sql"]

# 断联判定阈值：连续 N 天无消息视为一个断联窗口（转折点标记用）
GAP_DAYS = 14


class AnalyzeCancelled(Exception):
    """分析被用户中止（每个阶段之间检查一次，保证不会留下半截产物）"""


def _apply_all_schemas(conn) -> None:
    """建齐全部结构（幂等）——实现与依赖统一收在 phase5_common，避免两处清单漂移"""
    pc.apply_all_schemas(conn)


def _load_msgs_grouped(conn, session_gap_s: int | None = None) -> list[list[dict]]:
    """按 phase1 同款间隔重建会话分组（messages 表不存 session_id）；
    间隔默认取 config chat.session_gap_minutes"""
    if session_gap_s is None:
        session_gap_s = int(cfg_mod.load()["chat"]["session_gap_minutes"]) * 60
    rows = conn.execute(
        "SELECT ts, day, sender_key, subtype, content_clean, has_privacy "
        "FROM messages ORDER BY ts ASC").fetchall()
    msgs = [{"ts": r[0], "day": r[1], "sender": r[2], "subtype": r[3],
             "text": r[4] or "", "privacy": r[5] or 0} for r in rows]
    groups, cur = [], []
    for m in msgs:
        if cur and m["ts"] - cur[-1]["ts"] > session_gap_s:
            groups.append(cur)
            cur = []
        cur.append(m)
    if cur:
        groups.append(cur)
    return groups


# ---------------------------------------------------------------- 事件 → facts
def _next_event_id(conn) -> str:
    n = (conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] or 0) + 1
    return f"E{n:04d}"


def _next_fact_id(conn) -> str:
    n = (conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] or 0) + 1
    return f"F{n:04d}"


def _insert_event(conn, day, start_ts, end_ts, ev: dict, evidence: str = "") -> str:
    eid = _next_event_id(conn)
    conn.execute(
        "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, day, start_ts, end_ts, ev.get("event_type", ""), ev.get("summary", ""),
         int(ev.get("severity", 0)), evidence, pc.now_str()))
    return eid


def _insert_fact(conn, day, ev: dict, eid: str) -> None:
    summary = (ev.get("summary") or "").strip()
    if not summary:
        return
    sev = int(ev.get("severity") or 0)
    conf = {3: 0.8, 2: 0.7, 1: 0.6}.get(sev, 0.5)
    etype = ev.get("event_type", "事件")
    kw = pc.build_keywords_cn(ev.get("subject", ""), etype, summary)
    fid = _next_fact_id(conn)
    conn.execute(
        "INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, "episodic", ev.get("subject", "relationship"), etype, summary,
         f"{day} {etype}: {summary}", day, None, "events", eid, conf,
         1.0, None, 0, kw, "llm_checked" if ev.get("via") == "llm" else "pending",
         pc.now_str()))


def extract_events_llm(conn, client, max_sessions: int = 0,
                       progress: Callable[[str, str], None] | None = None,
                       should_cancel: Callable[[], bool] | None = None) -> int:
    """LLM 会话级事件抽取（phase2 SYSTEM_PROMPT + SessionSummary）"""
    from phase2_llm import SYSTEM_PROMPT, build_session_prompt
    from phase5_llm import SessionSummary

    groups = [g for g in _load_msgs_grouped(conn) if len(g) >= 2]
    if max_sessions:
        groups = groups[:max_sessions]
    n = 0
    for i, g in enumerate(groups):
        if should_cancel and should_cancel():
            raise AnalyzeCancelled("分析已中止（事件抽取阶段）")
        texts = [m for m in g if m["subtype"] in ("text", "quote_text")
                 and m["text"].strip() and not m["privacy"]]
        if len(texts) < 2:
            continue
        prompt = build_session_prompt(
            {"start_ts": g[0]["ts"], "end_ts": g[-1]["ts"]}, texts)
        day = g[0]["day"]
        if progress:
            try:
                progress("2", f"事件抽取：第 {i + 1}/{len(groups)} 个会话（已得 {n} 条）")
            except Exception:
                pass
        try:
            obj = client.extract(system=SYSTEM_PROMPT, user=prompt,
                                 response_model=SessionSummary)
        except Exception as e:
            print(f"  [event] 会话 {i + 1}/{len(groups)} 抽取失败（跳过）: {str(e)[:80]}")
            continue
        for ev in obj.events:
            d = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
            if not (d.get("summary") or "").strip():
                continue
            d.setdefault("subject", "relationship")
            d["via"] = "llm"
            eid = _insert_event(conn, day, g[0]["ts"], g[-1]["ts"], d)
            _insert_fact(conn, day, d, eid)
            n += 1
        conn.commit()
        if (i + 1) % 10 == 0:
            print(f"  [event] {i + 1}/{len(groups)} 会话完成，累计事件 {n}")
    return n


def extract_events_heuristic(conn) -> int:
    """零 token 启发式事件判定（复用对话内核的关键词规则表）"""
    from phase15_dial_engine import classify_event

    groups = [g for g in _load_msgs_grouped(conn) if len(g) >= 2]
    seen_day_types: set[tuple[str, str]] = set()
    n = 0
    for g in groups:
        for a, b in zip(g, g[1:]):
            if a["sender"] == b["sender"]:
                continue
            ev = classify_event(a["text"], b["text"])
            if not ev:
                continue
            key = (g[0]["day"], ev["event_type"])
            if key in seen_day_types:          # 同类事件每天只记一条
                continue
            seen_day_types.add(key)
            d = {**ev, "summary": f"{ev['event_type']}（启发式判定）",
                 "subject": "relationship", "via": "heuristic"}
            eid = _insert_event(conn, g[0]["day"], a["ts"], b["ts"], d)
            _insert_fact(conn, g[0]["day"], d, eid)
            n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- 关系状态估计
def estimate_relationship_state(conn) -> int:
    """逐月五维估计量：事件增量（tanh 压缩 + 回声）+ 断联窗口负漂移；基线中性 5.0"""
    eng = p6.RelEngine()
    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(day,1,7) FROM messages WHERE day IS NOT NULL ORDER BY 1")]
    if not months:
        return 0
    evs_by_month: dict[str, list[dict]] = {m: [] for m in months}
    # 注：events 表只存 severity；importance 由 p6.event_delta 在缺省时取中性值 3
    for day, etype, sev in conn.execute(
            "SELECT day, event_type, severity FROM events ORDER BY day"):
        m = (day or "")[:7]
        if m in evs_by_month:
            evs_by_month[m].append({"event_type": etype, "severity": sev or 3})
    active_months = {r[0] for r in conn.execute(
        "SELECT DISTINCT substr(day,1,7) FROM messages WHERE day IS NOT NULL")}
    gap_months = _gap_windows_months(conn)

    rel = {d: 5.0 for d in p6.DIMS}
    n = 0
    for m in months:
        net = {d: 0.0 for d in p6.DIMS}
        for ev in evs_by_month.get(m, []):
            for d, v in eng.event_delta(ev).items():
                net[d] += v
        if m in gap_months:                      # 断联窗口：整月零互动负漂移
            for d, v in p6.DEFAULT_RULES[p6.ABS_RULE_KEY]["dims"].items():
                net[d] += v
        elif m in active_months and not evs_by_month.get(m):
            net["closeness"] += 0.02             # 有互动但没抽出事件：温和正漂移
        for d in p6.DIMS:
            net_d = p6._compress(net[d])         # tanh 压缩防洪峰
            base = rel[d] * 0.98 + 5.0 * 0.02    # 轻微回归基线
            rel[d] = round(min(10.0, max(0.0, base + net_d)), 3)
        conn.execute(
            "INSERT OR REPLACE INTO relationship_state VALUES (?,?,?,?,?,?,?,?)",
            (m, rel["closeness"], rel["conflict"], rel["trust"],
             rel["emotional_safety"], rel["comm_quality"], 0.4, 0))
        n += 1
    conn.commit()
    return n


def _gap_windows_months(conn) -> set[str]:
    """断联窗口覆盖到的月份（按有消息日之间的空白段判定）"""
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT day FROM messages WHERE day IS NOT NULL ORDER BY 1")]
    out: set[str] = set()
    for a, b in zip(days, days[1:]):
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b)
        gap = (db - da).days
        if gap >= GAP_DAYS:
            cur = da
            while cur <= db:
                out.add(cur.strftime("%Y-%m"))
                cur += timedelta(days=1)
    return out


# ---------------------------------------------------------------- 转折点 / 岔路口
def detect_turning_points(conn) -> int:
    """三类标记：断联窗口（≥GAP_DAYS 天）、重要事件（sev*imp Top5）、活跃高峰月"""
    tps: list[dict] = []
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT day FROM messages WHERE day IS NOT NULL ORDER BY 1")]
    for a, b in zip(days, days[1:]):
        gap = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
        if gap >= GAP_DAYS:
            tps.append({"day": a, "range": f"{a}~{b}", "type": "activity_gap",
                        "title": f"断联 {gap} 天",
                        "description": f"{a} 之后进入 {gap} 天的无互动窗口，{b} 恢复联系"})
    top = conn.execute(
        "SELECT day, summary, severity FROM events "
        "ORDER BY COALESCE(severity, 0) DESC LIMIT 5").fetchall()
    for day, summary, sev in top:
        if not day:
            continue
        tps.append({"day": day, "range": day, "type": "event",
                    "title": (summary or "重要事件")[:24],
                    "description": f"重要事件（强度 {sev or 3}）：{summary}"})
    peak = conn.execute(
        "SELECT substr(day,1,7) AS m, COUNT(*) AS n FROM messages "
        "WHERE day IS NOT NULL GROUP BY m ORDER BY n DESC LIMIT 1").fetchone()
    if peak and peak[1] > 0:
        tps.append({"day": peak[0] + "-01", "range": peak[0], "type": "activity_anomaly",
                    "title": f"活跃高峰（{peak[0]}）",
                    "description": f"当月消息 {peak[1]} 条，为全期最高"})
    tps.sort(key=lambda t: t["day"])
    conn.execute("DELETE FROM turning_points")
    for i, t in enumerate(tps, start=1):
        conn.execute(
            "INSERT OR REPLACE INTO turning_points VALUES (?,?,?,?,?,?,?,?)",
            (f"TP{i:03d}", t["day"], t["range"], t["type"], t["title"],
             t["description"], "{}", ""))
    conn.commit()
    return len(tps)


# ---------------------------------------------------------------- persona
PERSONA_LAYERS = ("L", "M", "S", "U")
PERSONA_INTRO = {
    "L": "语言风格（L 层·时不变）：用词习惯、句长、标点/表情习惯、口头禅；"
         "其中客观可验证的条目 label 用【事实】，主观推断的用【推断】",
    "M": "压力与意义（M 层·中期）：近期在忙什么、压力源、在意什么",
    "S": "情绪与冲突模式（S 层·动态）：情绪起伏规律、冲突时的典型反应与修复方式",
    "U": "自我认知（U 层·用户校准真值，最优先遵守）：本人认可的自我描述/底线；拿不准可留空",
}


def _sample_messages(conn, person: str, per_bucket: int = 14) -> list[str]:
    """抽样某人的发言（首/中/末三段各取若干条，只取脱敏文本）"""
    rows = [r[0] for r in conn.execute(
        "SELECT content_clean FROM messages WHERE sender_key=? AND subtype IN "
        "('text','quote_text') AND has_privacy=0 AND TRIM(content_clean) <> '' "
        "ORDER BY ts", (person,)).fetchall()]
    if not rows:
        return []
    third = max(1, len(rows) // 3)
    buckets = (rows[:third], rows[third:2 * third], rows[2 * third:])
    out: list[str] = []
    for b in buckets:
        step = max(1, len(b) // per_bucket)
        out.extend(b[::step][:per_bucket])
    return out


def build_persona_llm(conn, client, person: str) -> bool:
    """LLM 生成 persona JSON（L/M/S/U 四层，供 build_world 加载）"""
    from phase2_llm import DeepSeekRepairClient  # noqa: F401 (确保已可用)
    from pydantic import BaseModel

    class _Item(BaseModel):
        label: str = ""
        item: str = ""

    class _Layer(BaseModel):
        items: list[_Item] = []

    class _PersonaDraft(BaseModel):
        display_name: str = ""
        layers: dict[str, _Layer] = {}

    samples = _sample_messages(conn, person)
    if not samples:
        print(f"  [persona/{person}] 没有可用的文本消息，跳过（可手写模板）")
        return False
    name = cfg_mod.person_display_name(person)
    joined = "\n".join(f"- {s[:120]}" for s in samples)
    sys_prompt = (
        "你是 IfWe 的人格档案生成器。根据某人在私聊中的发言抽样，产出 TA 的四层人格档案 JSON。"
        "只依据抽样内容与常识推断，禁止编造具体事件；每层 2~5 条、每条 ≤40 字。"
        + "；".join(f"{k}={v}" for k, v in PERSONA_INTRO.items()))
    user_prompt = (f"此人代号 {person}（界面显示名「{name}」）。发言抽样：\n{joined}\n\n"
                   f"请输出 JSON：{{\"display_name\": …, \"layers\": "
                   f"{{\"L\": {{\"items\": [{{\"label\": …, \"item\": …}}]}}, "
                   f"\"M\": …, \"S\": …, \"U\": …}}}}")
    try:
        obj = client.extract(system=sys_prompt, user=user_prompt,
                             response_model=_PersonaDraft)
    except Exception as e:
        print(f"  [persona/{person}] LLM 生成失败（改用模板）: {str(e)[:100]}")
        return build_persona_template(person)
    data = obj.model_dump()
    data["layers"] = {k: data["layers"].get(k, {"items": []}) for k in PERSONA_LAYERS}
    return _save_persona(person, data)


def persona_file_is_empty(path: Path) -> bool:
    """判断 persona JSON 是否还是空模板（display_name 与所有层 items 均为空）；
    文件缺失或解析失败按非空处理（避免误判后覆盖）"""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    if (d.get("display_name") or "").strip():
        return False
    for layer in (d.get("layers") or {}).values():
        for it in (layer.get("items") or []):
            if isinstance(it, dict) and (it.get("item") or "").strip():
                return False
    return True


def build_persona_template(person: str) -> bool:
    """手写模板：空档案 + 编辑指引（用户手填后即可跑对话）；
    已有手写档案（非空模板）时不覆盖，防止重跑分析抹掉用户填写内容"""
    pdir = cfg_mod.persona_dir()
    out = pdir / f"persona_v1_{person}.json"
    if out.exists() and not persona_file_is_empty(out):
        print(f"  [persona/{person}] 已有手写档案，跳过空模板覆盖"
              f"（想重置请删除 {out.name} 后重跑）")
        return True
    data = {
        "display_name": "",
        "_how_to_edit": "这是手写模板：把各层 items 填上（label 可用【事实】/【推断】），"
                        "display_name 填界面显示名。填写范例见 sample_data/persona_v1_*.json；"
                        "也可删掉本文件后用 LLM 路径重新生成。",
        "layers": {k: {"items": []} for k in PERSONA_LAYERS},
    }
    return _save_persona(person, data)


def _save_persona(person: str, data: dict) -> bool:
    pdir = cfg_mod.persona_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    out = pdir / f"persona_v1_{person}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [persona/{person}] 已写入 {out.relative_to(cfg_mod.root())}")
    return True


# ---------------------------------------------------------------- 主入口
def run_all(skip_llm: bool = False, max_sessions: int = 0,
            progress: Callable[[str, str], None] | None = None,
            should_cancel: Callable[[], bool] | None = None) -> dict:
    """跑完整分析管线。

    progress(code, message)   —— 阶段进度回调（阶段码 1~6，GUI 进度面板用）；
                                 原有的 [2]~[6] print 全部保留，CLI 输出不变
    should_cancel() -> bool   —— 阶段之间检查的中止标志位（GUI「中止」按钮用）
    """
    def _stage(code: str, msg: str) -> None:
        if progress:
            try:
                progress(code, msg)
            except Exception:
                pass

    def _check() -> None:
        if should_cancel and should_cancel():
            raise AnalyzeCancelled("分析已中止")

    t0 = time.time()
    _stage("1", "准备：加载配置与数据库")
    _check()
    conn = pc.connect()
    _apply_all_schemas(conn)
    n_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if not n_msgs:
        conn.close()
        raise SystemExit("库里还没有消息：先执行 python run.py init / python scripts/import_chat.py")
    _stage("1", f"准备完成：待分析消息 {n_msgs} 条")

    # 清空旧分析产物（幂等重建）
    for t in ("events", "facts", "relationship_state", "turning_points"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()

    client = None
    if not skip_llm:
        try:
            from phase5_llm import get_client
            client = get_client()
        except Exception as e:
            print(f"[warn] LLM 不可用（{str(e)[:80]}），自动切到离线路径")
            skip_llm = True

    _check()
    if skip_llm:
        n_ev = extract_events_heuristic(conn)
        print(f"[2] 事件（启发式）: {n_ev} 条")
    else:
        n_ev = extract_events_llm(conn, client, max_sessions=max_sessions,
                                  progress=progress, should_cancel=should_cancel)
        print(f"[2] 事件（LLM）: {n_ev} 条")
    _stage("2", f"事件抽取完成：{n_ev} 条")

    _check()
    n_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    print(f"[3] 记忆 facts: {n_facts} 条")
    _stage("3", f"记忆沉淀完成：{n_facts} 条")

    _check()
    n_rel = estimate_relationship_state(conn)
    print(f"[4] 关系状态: {n_rel} 个月（估计量）")
    _stage("4", f"关系状态估计完成：{n_rel} 个月")

    _check()
    n_tps = detect_turning_points(conn)
    print(f"[5] 转折点: {n_tps} 个（断联≥{GAP_DAYS}天 + 重要事件 + 活跃高峰）")
    _stage("5", f"转折点检测完成：{n_tps} 个")

    _check()
    persona_mode = "template" if skip_llm else "llm"
    for person in ("A", "B"):
        _check()
        _stage("6", f"人格档案：正在处理 {person}")
        ok = (build_persona_template(person) if skip_llm
              else build_persona_llm(conn, client, person))
        if not ok:
            build_persona_template(person)
    print(f"[6] persona: {persona_mode}（路径 {cfg_mod.persona_dir().relative_to(cfg_mod.root())}）")
    _stage("6", f"人格档案完成：{persona_mode}")
    conn.close()

    summary = {"events": n_ev, "facts": n_facts, "rel_months": n_rel,
               "turning_points": n_tps, "persona": persona_mode,
               "elapsed_s": round(time.time() - t0, 1), "skip_llm": skip_llm}
    print("\n分析完成。下一步: python run.py server（启动本地聊天界面）")
    return summary


if __name__ == "__main__":
    args = sys.argv[1:]
    run_all(skip_llm="--skip-llm" in args)
