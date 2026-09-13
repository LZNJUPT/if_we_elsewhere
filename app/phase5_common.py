# -*- coding: utf-8 -*-
"""
IfWe Phase 5 · A1 公共模块：模拟时钟 / 世界状态 / 分支库工具（本地，不依赖 LLM）
蓝图: 技术报告 §12 Agent Model（模拟时钟、随机种子、WorldState 快照）+ §10.1 结构
- SimClock: 快进/暂停/回退 + 固定随机种子（可复现）
- WorldState: 双人 persona_snapshot + relationship_state 只读视图 + facts 记忆视图(MemoryRetriever as_of) + 未提交决策点
- 分支库: sim_* 表只增不改（幂等：CREATE TABLE IF NOT EXISTS）

用法: 由对话内核（phase15_dial_engine）与 run.py analyze 调用
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import config as cfg_mod

ROOT = Path(__file__).resolve().parent.parent          # 项目根（app/ 的上一级）

# 以下路径/显示名在导入时取一次（兼容旧调用方）；切换好友或改配置后请调 refresh_paths()
DATA_DIR = cfg_mod.data_dir()
DB_PATH = cfg_mod.db_path()
RESULT_DIR = DATA_DIR / "phase5_results"
SCHEMA_PATH = Path(__file__).resolve().parent / "phase5_schema.sql"
PERSONA_DIR = cfg_mod.persona_dir()

# 显示名来自配置（people.A/B.display，用户自填或留空用代号）；内部逻辑一律用 A/B
SENDER_NAME = cfg_mod.sender_names()
SENDER_SHORT = {v: k for k, v in SENDER_NAME.items()}
DEFAULT_SEED = int(cfg_mod.load()["defaults"]["seed"])
EVENT_TYPES = [
    "见面/出行", "纪念日/承诺", "冲突/分歧", "冲突修复/和好", "情绪低谷/安慰",
    "高兴/庆祝", "学业考试/工作求职", "家人/朋友", "健康", "金钱/转账",
    "娱乐互动", "日常陪伴",
]


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def refresh_paths() -> None:
    """重新解析数据目录/人格目录/显示名（切换好友、改配置后调用）。

    同时把依赖本模块常量的下游模块（phase5_a2_loop / phase15_dial_engine）一起刷新，
    避免进程内缓存与新的 profile 数据目录不一致。
    """
    global DATA_DIR, DB_PATH, RESULT_DIR, PERSONA_DIR, SENDER_NAME, SENDER_SHORT, DEFAULT_SEED
    DATA_DIR = cfg_mod.data_dir()
    DB_PATH = cfg_mod.db_path()
    RESULT_DIR = DATA_DIR / "phase5_results"
    PERSONA_DIR = cfg_mod.persona_dir()
    SENDER_NAME = cfg_mod.sender_names()
    SENDER_SHORT = {v: k for k, v in SENDER_NAME.items()}
    DEFAULT_SEED = int(cfg_mod.load()["defaults"]["seed"])
    a2 = sys.modules.get("phase5_a2_loop")
    if a2 is not None:
        a2.SENDER_NAME = SENDER_NAME
    dial = sys.modules.get("phase15_dial_engine")
    if dial is not None:
        dial.AUTO_DAY_TURNS = int(cfg_mod.load()["defaults"]["auto_day_turns"])


def new_sim_id() -> str:
    return "SIM-" + time.strftime("%Y%m%d-%H%M%S")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(cfg_mod.db_path())        # 动态：随当前好友的数据目录切换
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def load_persona_json(person: str) -> dict:
    """读 phase3 定稿 persona JSON（L/M/S/U 四层 + 快照）"""
    with (cfg_mod.persona_dir() / f"persona_v1_{person}.json").open(encoding="utf-8") as f:
        return json.load(f)


def persona_layer_text(person: str, layer: str) -> str:
    """把某层 items 拼成提示用文本（只含派生画像，不含原文）"""
    p = load_persona_json(person)
    items = p.get("layers", {}).get(layer, {}).get("items", [])
    lines = []
    for it in items:
        txt = (it.get("item") or "").strip()
        if txt:
            lines.append(f"{it.get('label','')} {txt}")
    return "\n".join(lines)


def deliver_persona_block(person: str) -> str:
    """A2 用：组装 L/M/S/U 四层提示块（含用户主观真值 U 置顶提示）"""
    p = load_persona_json(person)
    name = (p.get("display_name") or "").strip() or cfg_mod.sender_names().get(person, person)
    blocks = [f"你是「{name}」，以下是你的长期人格档案（派生自真实关系分析，禁止推断档案之外的细节）："]
    layer_titles = {"L": "语言风格(L层·时不变)", "M": "压力与意义(M层·中期)",
                    "S": "情绪与冲突模式(S层·动态)", "U": "自我认知(U层·用户校准真值，最优先遵守)"}
    for lk in ("L", "M", "S", "U"):
        txt = persona_layer_text(person, lk)
        if txt:
            blocks.append(f"[{layer_titles[lk]}]\n{txt}")
    return "\n\n".join(blocks)


def persona_lite_block(person: str) -> str:
    """A2 每回合精简卡：风格要点(L【事实】优先) + S 冲突模式前几条 + U 层全部。
    目的：减少全量人格重复锚定（“人机感”来源之一），日常回合只提示高信号特征。"""
    p = load_persona_json(person)
    name = (p.get("display_name") or "").strip() or cfg_mod.sender_names().get(person, person)
    def items(lk):
        return p.get("layers", {}).get(lk, {}).get("items", [])
    l_fact = [(it.get("item", ""), it.get("label", "")) for it in items("L")
              if it.get("item") and it.get("label") == "【事实】"]
    l_infer = [(it.get("item", ""), it.get("label", "")) for it in items("L")
               if it.get("item") and it.get("label") != "【事实】" and len(l_fact) < 6]
    u = [(it.get("item", ""), it.get("label", "")) for it in items("U") if it.get("item")]
    s = [(it.get("item", ""), it.get("label", "")) for it in items("S") if it.get("item")][:3]
    lines = [f"你是「{name}」。提醒你的核心特征（简洁版）："]
    for txt, lbl in (l_fact + l_infer)[:6]:
        lines.append(f"- {lbl} {txt}")
    for txt, lbl in s:
        lines.append(f"- [S] {lbl} {txt}")
    if u:
        lines.append("【自我认知(U层·最优先)】")
        for txt, lbl in u:
            lines.append(f"- {lbl} {txt}")
    return "\n".join(lines)


def load_relationship_state(conn: sqlite3.Connection) -> list[dict]:
    """relationship_state 全量只读（供 WorldState 视图 / A6 轨迹比较）"""
    rows = conn.execute(
        "SELECT period, closeness, conflict, trust, emotional_safety, comm_quality, confidence "
        "FROM relationship_state ORDER BY period").fetchall()
    out = []
    for p, cl, cf, tr, es, cq, conf in rows:
        out.append({"period": p, "closeness": cl, "conflict": cf, "trust": tr,
                    "emotional_safety": es, "comm_quality": cq, "confidence": conf})
    return out


def rel_state_at(conn: sqlite3.Connection, day: str) -> Optional[dict]:
    """按日期取最近一期关系状态（只读视图，供 Agent 读取）"""
    period = day[:7]
    rows = conn.execute(
        "SELECT period, closeness, conflict, trust, emotional_safety, comm_quality, confidence "
        "FROM relationship_state WHERE period <= ? ORDER BY period DESC LIMIT 1", (period,)).fetchall()
    if not rows:
        return None
    p, cl, cf, tr, es, cq, conf = rows[0]
    return {"period": p, "closeness": cl, "conflict": cf, "trust": tr,
            "emotional_safety": es, "comm_quality": cq, "confidence": conf}


# ---------------- A1 模拟时钟 ----------------
@dataclass
class SimClock:
    """可控模拟时钟：步进 / 快进 / 暂停 / 回退 / 分支；固定随机种子保证可复现"""
    start_day: str
    seed: int = 0               # 0 = 使用配置 defaults.seed
    step_days: int = 1

    def __post_init__(self):
        self.current = date.fromisoformat(self.start_day)
        self._end = None            # 无 set_end 则无界
        self._paused = False
        self._history: list[str] = [self.start_day]   # day 序列（便于回退）
        self.rng = random.Random(self.seed or DEFAULT_SEED)

    @property
    def day(self) -> str:
        return self.current.isoformat()

    def forward(self, days: int | None = None) -> str:
        """快进 days 天（默认 step_days），写入历史返回新日期"""
        if self._paused:
            return self.day
        n = days if days is not None else self.step_days
        for _ in range(n):
            if self._end and self.current >= self._end:
                break
            self.current += timedelta(days=1)
            self._history.append(self.day)
        return self.day

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def rewind(self, to_day: Optional[str] = None, steps: int = 0) -> str:
        """回退到指定日期或回退 steps 步（保留历史供恢复）"""
        if to_day:
            try:
                self.current = date.fromisoformat(to_day)
            except Exception:
                raise ValueError(f"非法日期: {to_day}")
        else:
            for _ in range(min(steps, max(0, len(self._history) - 1))):
                self._history.pop()
            self.current = date.fromisoformat(self._history[-1])
        return self.day

    def set_end(self, end_day: str) -> None:
        self._end = date.fromisoformat(end_day)

    def next_int(self, lo: int, hi: int) -> int:
        """放种子决定的确定性随机（用于谁先开口等）"""
        return self.rng.randint(lo, hi)


# ---------------- WorldState ----------------
@dataclass
class WorldState:
    """§10.1 结构：当前世界的一帧快照（只读视图 + 未提交决策点）"""
    sim_id: str
    branch_name: str
    branch_of: str
    divergence_point: Optional[str]
    clock: "SimClock"
    divergence_desc: str = ""
    person_names: dict = field(default_factory=dict)   # {"A": 显示名A, "B": 显示名B}
    persona_blocks: dict = field(default_factory=dict) # {"A": <L/M/S/U 提示块>, "B": ...}
    rel_state: Optional[dict] = None                   # relationship_state 只读视图（截至当日）
    memories: list[dict] = field(default_factory=list)   # MemoryRetriever as_of 检索视图缓存
    uncommitted: list[dict] = field(default_factory=list) # 待用户改写的决策点（A4 用）
    rewritten_choice: Optional[str] = None
    next_event_seq: int = 0

    @property
    def day(self) -> str:
        return self.clock.day

    def divergence_scene(self) -> str:
        """分歧日场景卡：改写作为『当下正在发生的事』注入第一个回合（A4 用，只触发一次）"""
        if not (self.divergence_point and self.day >= self.divergence_point
                and not getattr(self, "_scene_fired", False)):
            return ""
        self._scene_fired = True   # 只注入一次
        return (f"【最重要·改写决定】在 {self.divergence_point} 这个决定时刻，现实被改写了："
                f"{self.divergence_desc or '选择不同'}。"
                f"结果：{self.rewritten_choice or '（选择不同）'}。"
                f"今天是改写后的第一天，请把今天的互动**从回应这个决定开始**"
                f"（可以是开场白、回应或行动的暗示），必须符合两人真实人格与说话风格——"
                f"即使 B 依然怕、依然谨慎，TA 的选择已经不同于以往，要在言行中体现出来。")

    def summary_text(self, with_memories: bool = True) -> str:
        """拼给 Agent 的『当前世界状态』文本（派生，只读视图）"""
        lines = [f"今天是 {self.day}，关系分支「{self.branch_name}」。"]
        if self.divergence_point and self.day >= self.divergence_point:
            lines.append(f"【最重要·改写已生效】{self.divergence_desc or '改写已生效'} "
                         f"→ {self.rewritten_choice or ''}")
        if self.rel_state:
            r = self.rel_state
            lines.append("当前关系状态（截至 {}，只读）：亲密度={} 冲突指数={} 信任={} "
                         "情绪安全={} 沟通质量={}（置信度{}）".format(
                r["period"], r["closeness"], r["conflict"], r["trust"],
                r["emotional_safety"], r["comm_quality"], r["confidence"]))
        if with_memories and self.memories:
            lines.append("此刻浮现的记忆：")
            for m in self.memories[:6]:
                lines.append(f"- [{m.get('memory_type','')}] {m.get('conclusion_text','')[:70]}")
        return "\n".join(lines)


def build_world(conn: sqlite3.Connection, start_day: str, *, seed: int = 0,
                branch_name: str = "main", branch_of: str = "main",
                divergence_point: Optional[str] = None, divergence_desc: str = "",
                rewritten_choice: Optional[str] = None,
                sim_id: Optional[str] = None,
                memory_view_days: int = 60) -> WorldState:
    """从 start_day 冻结世界（seed=0 时取配置 defaults.seed）：
    - persona 双人 L/M/S/U 提示块
    - relationship_state 截至 start_day 的只读视图
    - facts 记忆视图：同步建 MemoryRetriever 占位（真正检索由 A2 注入）
    """
    clock = SimClock(start_day=start_day, seed=seed or DEFAULT_SEED)
    ws = WorldState(
        sim_id=sim_id or new_sim_id(),
        branch_name=branch_name, branch_of=branch_of,
        divergence_point=divergence_point, divergence_desc=divergence_desc,
        rewritten_choice=rewritten_choice,
        clock=clock,
        person_names=dict(cfg_mod.sender_names()),
        persona_blocks={"A": deliver_persona_block("A"), "B": deliver_persona_block("B")},
        rel_state=rel_state_at(conn, start_day),
    )
    # memory 视图由 A2 每回合注入（MemoryRetriever.search as_of=当前日）
    ws.memories = []
    return ws


def summary_json(ws: WorldState) -> dict:
    return {
        "sim_id": ws.sim_id, "branch_name": ws.branch_name, "branch_of": ws.branch_of,
        "divergence_point": ws.divergence_point, "divergence_desc": ws.divergence_desc,
        "rewritten_choice": ws.rewritten_choice,
        "person_names": ws.person_names,
        "clock": {"start_day": ws.clock.start_day, "current_day": ws.day,
                  "seed": ws.clock.seed, "step_days": ws.clock.step_days,
                  "history": ws.clock._history[-5:]},
        "rel_state_snapshot": ws.rel_state,
        "persona_layers_loaded": {"A": list(("L", "M", "S", "U")), "B": list(("L", "M", "S", "U"))},
    }


# ---------------- 分支库工具（幂等） ----------------
def init_sim(conn: sqlite3.Connection, ws: WorldState, n_rounds: int,
             config: dict | None = None, status: str = "running") -> str:
    """建 sim_runs 主记录，返回 sim_id"""
    sid = ws.sim_id
    cfg = config or {}
    conn.execute(
        """INSERT OR REPLACE INTO sim_runs
           (sim_id, branch_name, branch_of, divergence_point, divergence_desc, rewritten_choice,
            start_day, end_day, n_rounds, clock_seed, config_json, status, created_at)
           VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,?)""",
        (sid, ws.branch_name, ws.branch_of, ws.divergence_point, ws.divergence_desc,
         ws.rewritten_choice, ws.clock.start_day, n_rounds, ws.clock.seed,
         json.dumps(cfg, ensure_ascii=False), status, now_str()))
    conn.commit()
    return sid


def finish_sim(conn: sqlite3.Connection, sim_id: str, end_day: str, status: str = "done") -> None:
    conn.execute("UPDATE sim_runs SET end_day=?, status=? WHERE sim_id=?", (end_day, status, sim_id))
    conn.commit()


# ---------------- 分支记忆检索视图（A2 用：主 facts + 本分支 sim_facts 融合） ----------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALNUM_RE = re.compile(r"[A-Za-z0-9#+._/\\-]{2,}")


def build_keywords_cn(*parts: str) -> str:
    """sim_facts 检索词：字符 bigram + 连续非中文 token（与主 facts keywords 同规则）"""
    toks: set[str] = set()
    for p in parts:
        p = p or ""
        cjk = "".join(_CJK_RE.findall(p))
        toks.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
        toks.update(_ALNUM_RE.findall(p))
    return " ".join(sorted(t for t in toks if t))


def search_branch_memory(retriever, conn, sim_id: str, query: str, as_of: str,
                         top_k: int = 5, main_top: int = 15, frozen_at: str | None = None) -> list[dict]:
    """RRF 融合：MemoryRetriever(主 facts, as_of 双时态) + 本分支 sim_facts（bigram 重叠打分）。
    让 Agent 既能召回真实历史记忆，也能召回本分支此前续演产生的事件/摘要记忆。
    frozen_at: 反事实冻结点（§10.4）——分歧点之后，主记忆按分歧点截断 as_of，
               确保 Agent 看不到被改写掉的真实未来。"""
    from phase4_retrieval import query_tokens
    toks = list(dict.fromkeys(query_tokens(query)))
    # 1) 主记忆（真实历史；分歧冻结时 as_of 截断到 frozen_at）
    main_as_of = frozen_at or as_of
    try:
        main = retriever.search(query, top_k=main_top, as_of=main_as_of,
                                apply_forgetting=True, mode="hybrid")
    except Exception:
        main = []
    # 2) 分支记忆（sim_facts）
    rows = conn.execute(
        "SELECT fact_id, memory_type, subject, relation, object, conclusion_text, "
        "valid_at, invalid_at, confidence, keywords FROM sim_facts WHERE sim_id=?",
        (sim_id,)).fetchall()
    sim = []
    for fid, mtype, subj, rel, obj, txt, va, iva, conf, kw in rows:
        kwtoks = set((kw or "").split())
        matched = [t for t in toks if t in (txt or "") or t in kwtoks]
        rel_score = len(matched) / max(1, len(toks)) if toks else 0.0
        if rel_score == 0 and not kwtoks:
            continue
        sim.append({"fact_id": fid, "memory_type": mtype, "subject": subj,
                    "relation": rel, "object": obj, "conclusion_text": txt,
                    "valid_at": va, "invalid_at": iva, "source": "simulation",
                    "source_id": fid, "confidence": float(conf or 0.5),
                    "relevance": round(rel_score, 3), "score": round(rel_score, 3)})
    sim.sort(key=lambda x: (x["relevance"], x["confidence"]), reverse=True)
    sim = sim[:10]
    # 3) RRF 融合（k=60）
    RRF_K = 60
    fusion: dict[str, dict] = {}
    for i, r in enumerate(main):
        f = fusion.get(r["fact_id"], dict(r))
        f["_rrf"] = f.get("_rrf", 0.0) + 1.0 / (RRF_K + i + 1)
        fusion[r["fact_id"]] = f
    for i, r in enumerate(sim):
        f = fusion.get(r["fact_id"], dict(r))
        f["_rrf"] = f.get("_rrf", 0.0) + 1.0 / (RRF_K + i + 1)
        fusion[r["fact_id"]] = f
    ranked = sorted(fusion.values(), key=lambda x: x["_rrf"], reverse=True)
    for r in ranked:
        r.pop("_rrf", None)
    return ranked[:top_k]


def branch_memory_text(rows: list[dict]) -> str:
    """分支记忆 → 提示文本"""
    lines = []
    for r in rows:
        va = r.get("valid_at") or "—"
        iv = r.get("invalid_at") or "今"
        tag = "分支" if r.get("source") == "simulation" else "历史"
        lines.append(f"[{tag}|{r.get('memory_type','')}|{va}~{iv}] {r.get('conclusion_text','')[:80]}")
    return "\n".join(lines)


if __name__ == "__main__":
    pass