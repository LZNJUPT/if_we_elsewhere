# -*- coding: utf-8 -*-
"""
IfWe Phase 6 · Relationship Engine v1 核心模块（本地，不依赖 LLM）
蓝图: docs/ARCHITECTURE.md（三元结构：R 稳态基线 / R 动态事件驱动 / R 快照序列）
      + §12（关系状态=双 Agent 共享对象）+ Phase5 启发式升级
口径:
  - R 稳态基线: 真实 44 期月度快照的缓慢变化基线（EWMA），来自 facts(memory_type=state, source=relationship_state)
  - R 动态: 12 类事件 × (delta 近端, decay/echo) → 月粒度净增量（tanh 压缩）+ 断联零互动负漂移
  - R 快照序列: 每个 period/day 的 engine_state 落独立 rel_engine_state 表（R3 只读+独立表，不碰主库真值）
  - M7 校准系数: closeness×1.25 / trust×1.20 / emotional_safety×1.25 / comm_quality×1.15 / conflict×0.85
用法:
  python phase6_engine.py --emit-r1   # 产出 r1_rule_model.json + r1_design.md
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "ifwe_v1.db"
RESULT_DIR = DATA_DIR / "phase6_results"

DIMS = ["closeness", "conflict", "trust", "emotional_safety", "comm_quality"]
DIM_LABEL = {"closeness": "亲密度", "conflict": "冲突指数", "trust": "信任",
             "emotional_safety": "情绪安全", "comm_quality": "沟通质量"}

# ---- Phase2 M7 用户校准系数（§3 R1 明确要求带入增量模型）----
CALIB_FACTORS = {
    "closeness": 1.25,
    "trust": 1.20,
    "emotional_safety": 1.25,
    "comm_quality": 1.15,
    "conflict": 0.85,
}

# ---- R1 规则表（12 类事件 + 断联零互动）: delta 为 sev=3/imp=3 处的基础增量（未乘 M7 系数，月/日两用）----
DEFAULT_RULES = {
    "日常陪伴": {"dims": {"closeness": 0.015, "comm_quality": 0.015, "emotional_safety": 0.010},
                "reason": "日常陪伴是关系保温的涓流：频繁互动缓慢累积亲密与沟通顺畅，情绪安全感随之微升。"},
    "见面/出行": {"dims": {"closeness": 0.050, "comm_quality": 0.035},
                "reason": "线下见面/共同出行是关系升温的高信号动作：亲密与沟通质量获得即时且明显的正增量。"},
    "娱乐互动": {"dims": {"closeness": 0.030, "comm_quality": 0.030, "emotional_safety": 0.020},
                "reason": "共同娱乐（游戏/视频/梗）创造轻松的共同经历：亲密与沟通顺畅度温和上升。"},
    "学业考试/工作求职": {"dims": {"comm_quality": 0.030, "closeness": 0.015},
                "reason": "学业/求职是两人高频互助话题：共同应对现实压力提升沟通质量，也带来轻度联结。"},
    "健康": {"dims": {"emotional_safety": -0.035, "closeness": 0.015},
            "reason": "健康波动引入担忧（情绪安全小幅回落），同时健康关切会强化亲密联结。"},
    "情绪低谷/安慰": {"dims": {"emotional_safety": 0.050, "closeness": 0.030, "trust": 0.015},
                "reason": "低谷被看见并得到安慰：情绪安全显著回升，亲密与信任伴随安慰行为小幅增强（参考 Phase5 启发式）。"},
    "冲突/分歧": {"dims": {"conflict": 0.080, "closeness": -0.050, "emotional_safety": -0.050,
                          "trust": -0.040, "comm_quality": -0.035},
                "reason": "冲突直接推高冲突指数，并连带削弱亲密、情绪安全、信任与沟通质量（短期负面四联）。"},
    "冲突修复/和好": {"dims": {"conflict": -0.100, "closeness": 0.050, "trust": 0.040,
                              "emotional_safety": 0.050, "comm_quality": 0.050},
                "reason": "修复/和好是关系韧性信号：冲突指数大幅回落，亲密/信任/情绪安全/沟通质量同步回升。"},
    "家人/朋友": {"dims": {"closeness": 0.020, "trust": 0.020},
                "reason": "把家人/朋友话题带进关系意味着信任边界的扩展，亲密与信任温和上升。"},
    "纪念日/承诺": {"dims": {"closeness": 0.060, "trust": 0.050},
                "reason": "纪念日/承诺是稳定与未来的锚点：亲密与信任获得较强的正增量。"},
    "高兴/庆祝": {"dims": {"closeness": 0.040, "emotional_safety": 0.035},
                "reason": "共同庆祝放大正性情绪：亲密与情绪安全同步上升。"},
    "金钱/转账": {"dims": {"trust": 0.040, "conflict": -0.015},
                "reason": "金钱往来是信任信号（愿意托付/不设防），轻微缓和冲突。"},
    # 合成事件：整月零互动（断联窗口）——零互动月的线上表露断崖，状态回落
    "断联/无互动": {"dims": {"closeness": -0.250, "trust": -0.120, "comm_quality": -0.300,
                            "emotional_safety": -0.080, "conflict": 0.0},
                "reason": ("整月无有效互动（断联窗口）：线上活跃度与情感表露断崖，亲密度/沟通质量/信任"
                           "回落为'线上互动缺失'（用户校准：断联为现实因素制约而非情感降温，冲突指数不变）。"),
                "synthetic": True},
}

ECHO_DECAY_DEFAULT = 0.45      # 月粒度回声衰减（近端影响大、远期回声递减，§9 R 动态）
ECHO_HORIZON = 6               # 回声视窗（月）
STEADY_ALPHA = 0.5             # 稳态基线 EWMA 权重（真实前任期权重）
COMPRESS_GAIN = 1.2            # 月粒度净增量 tanh 压缩增益（抑制事件洪峰过度累积）
SEV_GAIN = 0.15                # severity 超出中心值(3)每级 ±15%
IMP_GAIN = 0.08                # importance 超出中心值(3)每级 ±8%
SCALE_CLIP = (0.4, 1.8)
CLAMP = (0.0, 10.0)

ABS_RULE_KEY = "断联/无互动"


def _compress(x: float, gain: float = COMPRESS_GAIN) -> float:
    """月粒度净增量压缩：小流量近似线性、大流量饱和（防重复事件洪峰累积）"""
    g = gain or 1.0
    return g * math.tanh(x / g) if x else 0.0


def load_real_states(conn) -> dict[str, dict]:
    """真实 44 期（calibrated 真值，只读）"""
    rows = conn.execute(
        "SELECT period, closeness, conflict, trust, emotional_safety, comm_quality, confidence "
        "FROM relationship_state ORDER BY period").fetchall()
    out = {}
    for p, cl, cf, tr, es, cq, conf in rows:
        out[p] = {"closeness": cl, "conflict": cf, "trust": tr,
                  "emotional_safety": es, "comm_quality": cq, "confidence": conf}
    return out


@dataclass
class RelEngine:
    """关系引擎 v1：R 稳态 + R 动态（事件增量/衰减/回声）+ 快照序列
    center=True: 月动态相对"典型月"去中心化（a priori 版，稳态承接水平）
    center=False: 依赖规则自带的截距/权重视为已去中心（v1c 数据校准版，交互由截距承担）"""
    rules: dict = field(default_factory=lambda: DEFAULT_RULES)
    calib: dict = field(default_factory=lambda: CALIB_FACTORS)
    echo_decay: float = ECHO_DECAY_DEFAULT
    echo_horizon: int = ECHO_HORIZON
    steady_alpha: float = STEADY_ALPHA
    compress_gain: float = COMPRESS_GAIN
    center: bool = True

    # ---------- 事件 → 增量 ----------
    def event_delta(self, ev: dict) -> dict:
        """单事件增量向量（含 M7 校准系数与 sev/imp 缩放）；返回 {dim: 增量, ...}"""
        t = ev.get("event_type", "")
        rule = self.rules.get(t)
        if not rule:
            return {}
        sev = float(ev.get("severity") or 0)
        imp = float(ev.get("importance") or 0)
        sev = 3.0 if sev <= 0 else sev
        imp = 3.0 if imp <= 0 else imp
        scale = 1.0 + SEV_GAIN * (sev - 3.0) + IMP_GAIN * (imp - 3.0)
        scale = max(SCALE_CLIP[0], min(SCALE_CLIP[1], scale))
        out = {}
        for dim, raw in rule.get("dims", {}).items():
            out[dim] = round(raw * scale * self.calib.get(dim, 1.0), 5)
        return out

    def reason_event(self, ev: dict, delta: dict) -> str:
        """单事件解释链文本（供可视化/评测透传）：某事件为何改变某维"""
        t = ev.get("event_type", "")
        rule = self.rules.get(t, {})
        sev = float(ev.get("severity") or 3)
        imp = float(ev.get("importance") or 3)
        parts = [f"{t}(sev={int(sev)},imp={int(imp)}): {rule.get('reason', '')}"]
        for dim in DIMS:
            if dim in delta and delta[dim]:
                parts.append(f"→{DIM_LABEL[dim]}{'+' if delta[dim] > 0 else ''}{delta[dim]:.3f}")
        return " ".join(parts)

    # ---------- 月粒度动态 ----------
    def month_dynamics(self, events_by_month: dict[str, list[dict]],
                       months: list[str]) -> dict[str, dict]:
        """每月净动态增量（含回声叠加与断联零互动漂移），输出相对"典型月"去中心化的扰动。
        去中心化理由: 大部分事件类型是正向（日常陪伴/见面/娱乐…），原始月净增量整体为正，
        会系统性抬高状态；稳态基线已承接真值水平，动态层只应表达"偏离典型月"的短时张力。"""
        raw: dict[str, dict] = {m: {d: 0.0 for d in DIMS} for m in months}
        for m, evs in events_by_month.items():
            if m not in raw:
                raw[m] = {d: 0.0 for d in DIMS}
            for ev in evs:
                dl = self.event_delta(ev)
                for d, v in dl.items():
                    raw[m][d] += v
            if not evs:
                for d, v in (self.rules[ABS_RULE_KEY]["dims"]).items():
                    raw[m][d] += v
        # 回声叠加：远期事件的衰减影响（echo，不压缩的原始量×衰减^k）
        echo: dict[str, dict] = {m: {d: 0.0 for d in DIMS} for m in months}
        idx = {m: i for i, m in enumerate(months)}
        for m in months:
            i = idx[m]
            for k in range(1, min(self.echo_horizon, i) + 1):
                pm = months[i - k]
                decay = self.echo_decay ** k
                for d in DIMS:
                    echo[m][d] += raw[pm][d] * decay
        # 压缩净增量（小月近似线性，洪峰月饱和）
        comp: dict[str, dict] = {m: {d: round(_compress(raw[m][d] + echo[m][d],
                                                        self.compress_gain), 5) for d in DIMS}
                                 for m in months}
        if not self.center:
            # v1c 数据校准版：截距已承担典型月流量，直接返回压缩净增量
            return comp
        # a priori 版去中心化: 用前 W 个月估计"典型月流量"（仅用早期数据，不泄漏未来）
        warm = max(1, len(months) // 4)
        warm_months = months[:warm]
        center = {d: round(sum(comp[m][d] for m in warm_months) / len(warm_months), 5)
                  for d in DIMS}
        net = {m: {d: round(comp[m][d] - center[d], 5) for d in DIMS} for m in months}
        return net

    # ---------- R 稳态基线 ----------
    def steady_series(self, real: dict[str, dict], months: list[str]) -> dict[str, dict]:
        """缓慢变化基线：前向 EWMA（不强用未来，诚实于预测）"""
        out = {}
        prev = None
        for m in months:
            r = real.get(m)
            if prev is None:
                if r:
                    prev = {d: r[d] for d in DIMS}
                    out[m] = dict(prev)
                continue
            # 稳态回归：0.5×最近真实 + 0.5×前一稳态
            if r:
                nxt = {d: round(self.steady_alpha * r[d] + (1 - self.steady_alpha) * prev[d], 5)
                       for d in DIMS}
            else:
                nxt = {d: round(prev[d] * 0.99, 5) for d in DIMS}   # 无真值月：缓慢自然漂移
            out[m] = nxt
            prev = nxt
        return out

    # ---------- 快照序列 ----------
    def trajectory(self, real: dict[str, dict], events_by_month: dict[str, list[dict]],
                   months: list[str], mode: str = "rolling") -> dict[str, dict]:
        """engine_state 快照序列（R 三元结构→R 快照）。
        mode=rolling: 稳态取自身前值（h 期滚动，不引用未来真实）
        mode=one_step: 稳态=真实前任期的 EWMA（再锚定预测，隔离增量模型本身质量）"""
        steady = None
        if mode == "one_step":
            steady = self.steady_series(real, months)
        dyn = self.month_dynamics(events_by_month, months)
        out, prev = {}, None
        for m in months:
            base = steady[m] if steady else (prev or {d: (real.get(months[0]) or {}).get(d, 5.0)
                                                      for d in DIMS})
            nxt = {}
            for d in DIMS:
                b = base[d]
                nxt[d] = round(min(CLAMP[1], max(CLAMP[0], b + dyn[m][d])), 4)
            out[m] = nxt
            prev = nxt
        return out

    # ---------- 解释链（可解释透传，供 R5/R6/R7）----------
    def explain_month(self, events_by_month: dict, events: list[dict], period: str,
                      before: dict, after: dict, top_n: int = 6) -> list[dict]:
        chain = []
        for ev in events:
            dl = self.event_delta(ev)
            if not dl:
                continue
            chain.append({"event_type": ev.get("event_type", ""), "summary": ev.get("summary", ""),
                          "severity": int(ev.get("severity") or 3), "importance": int(ev.get("importance") or 3),
                          "deltas": dl,
                          "reason": self.reason_event(ev, dl)})
        chain.sort(key=lambda x: -max(map(abs, x["deltas"].values())))
        return {
            "period": period,
            "n_events": len(events),
            "before": before, "after": after,
            "dim_diffs": {d: round(after[d] - before.get(d, 0), 4) for d in DIMS},
            "drivers": chain[:top_n],
        }


# ---------------- R4 分支（日粒度）接入：drop-in 替换 phase5_a2_loop.update_rel_state ----------------
def update_rel_state_engine(conn, sim_id: str, day: str, prev: dict, events: list[dict],
                            anchor: Optional[dict] = None) -> dict:
    """正式模型版分支内状态更新（替代启发式）。日粒度：不加月压缩，事件增量直加 + 缓回归稳态锚点。
    写入 sim_rel_state (method='engine')，供后续续演分支的 sim_rel_state 使用（R4）。"""
    eng = RelEngine()
    s = dict(prev or {})
    for ev in events:
        dl = eng.event_delta(ev)
        for d, v in dl.items():
            s[d] = s.get(d, 5.0) + v
    # 日粒度缓回归：0.99 向锚点（世界开局的只读真值/或传入），代替原启发式的固定基线
    base = anchor or {"closeness": 5.0, "conflict": 5.0, "trust": 5.0,
                      "emotional_safety": 5.0, "comm_quality": 5.0}
    for d in DIMS:
        base_v = base.get(d) or 5.0
        s[d] = round(min(10.0, max(0.0, 0.99 * s.get(d, base_v) + 0.01 * base_v)), 3)
    notes = "engine_v1 日粒度：规则表增量(M7校准) + 0.99稳态回归锚点"
    conn.execute(
        """INSERT OR REPLACE INTO sim_rel_state
           (sim_id, day, closeness, conflict, trust, emotional_safety, comm_quality,
            confidence, method, notes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sim_id, day, s["closeness"], s["conflict"], s["trust"],
         s["emotional_safety"], s["comm_quality"], 0.45, "engine", notes))
    conn.commit()
    return s


def rule_table_json() -> dict:
    return {"version": "v1", "calibrated_dims": CALIB_FACTORS,
            "echo": {"decay": ECHO_DECAY_DEFAULT, "horizon_months": ECHO_HORIZON},
            "steady": {"ewma_alpha": STEADY_ALPHA, "source": "facts(memory_type=state, source=relationship_state)"},
            "compress": {"gain": COMPRESS_GAIN, "note": "月粒度净增量 tanh 压缩"},
            "scale": {"sev_gain": SEV_GAIN, "imp_gain": IMP_GAIN, "clip": list(SCALE_CLIP)},
            "clamp": list(CLAMP),
            "events": {t: {"dims": r["dims"], "reason": r["reason"],
                           "synthetic": r.get("synthetic", False)}
                       for t, r in DEFAULT_RULES.items()}}


DESIGN_MD = """# Phase 6 · R1 事件→状态增量模型设计（Relationship Engine v1 规则表）

> 模型版本: engine_v1 ｜ 全部本地可重跑（不依赖 LLM）

【事实】输入来源（均来自用户自己的库，analyze 阶段生成）
- R 稳态基线: `relationship_state` 逐月快照（`facts` 中 memory_type=state 的只读视图同源）
- R 动态: 主库 `events`（12 类）/ `sim_events`（分支）
- 校准系数: 用户可校准的维度权重（默认 closeness×1.25 / trust×1.20 / emotional_safety×1.25 /
  comm_quality×1.15 / conflict×0.85，v0.1 内置先验值，可在 analyze 后人工微调）

【推断】规则表结构（12 类事件 × delta + 1 合成断联规则）
每类事件对五维（亲密度/冲突/信任/情绪安全/沟通质量）给出基础增量，理由示例如下：
- 日常陪伴：保温涓流，小幅正增量
- 见面/出行：线下高信号，近端强增量 + 回声衰减
- 冲突/分歧：短期负面四联（冲突升、亲密/信任/情绪安全/沟通降）
- 冲突修复/和好：修复即韧性，冲突大幅回落
- 断联/无互动（合成）：整月零互动 → 线上表露断崖（冲突不变：无互动 ≠ 情感降温）

更新机制: 事件类型量化 → 状态增量（echo 回声 = 上月净动态 × 0.45^k，k ≤ 6 月）→ 快照落库
- 缩放: severity/importance 中心 3 级，每级 ±8~15%，裁剪 [0.4,1.8]
- 月粒度净增量做 tanh 压缩（gain=1.2）：小月近似线性、洪峰月饱和
- 数值裁剪 [0,10]

【推断】设计取舍
- 拒绝单值伪精确：输出 5 维向量 + 每条解释链，不做单值综合。
- 稳态 vs 动态分离：稳态（EWMA 前向,α=0.5）承接基线水平；动态只表达事件引起的偏离。
"""


def emit_r1() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "r1_rule_model.json").write_text(
        json.dumps(rule_table_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULT_DIR / "r1_design.md").write_text(DESIGN_MD, encoding="utf-8")
    print("r1_rule_model.json / r1_design.md ->", RESULT_DIR)


if __name__ == "__main__":
    import sys
    if "--emit-r1" in sys.argv:
        emit_r1()
    else:
        # 冒烟：单事件解释
        eng = RelEngine()
        ev = {"event_type": "冲突/分歧", "severity": 4, "importance": 3, "summary": "一场争执",
              "day": "2026-07-10"}
        dl = eng.event_delta(ev)
        print(eng.reason_event(ev, dl))