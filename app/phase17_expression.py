# -*- coding: utf-8 -*-
"""Phase 17 · 表达层条件化（二期方案 v2 §11 Phase E：风格/长度/媒介/延迟/主动开口）

§11 Phase E 定位：最后才处理的表达层，不能代替立场与行动层。
全部参数从真实数据推导（无手写常数），样本量与出处随 profile 一并返回：

  - medium_probs     ：真实 B 消息媒介分布（当前阶段段；§10.6 静态概率的条件化第一步——
                       按【阶段】条件化已落地；按【行动模式】条件化缺真实标注，如实标注限制）；
  - length_quantiles ：真实 B 消息长度分位 P10/P40/P50/P90（实测双峰：短敷衍 + 长文表态）；
  - delay_quantiles  ：paired 认领对 A→B 间隔的 P33/P66（分钟；delay_bucket 分档边界）；
  - initiation_rate  ：真实 B 先开口天占比（主动开口掷骰概率，数据即出处）。

观测口径（§13.3-5 方差门禁）：长度指标必须报告 sd，不得只报均值。
分支内不使用模型自身输出更新 profile（同 §7.2 阶段纪律）——只读真实 messages。
"""
from __future__ import annotations

import sqlite3

import phase17_phase as ph

PROFILE_VERSION = "expression_mvp_v1"


def _quantile(sorted_vals: list, q: float):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[idx]


def expression_profile(conn: sqlite3.Connection, as_of: str) -> dict:
    """当前阶段段的 B 表达画像（全部真实数据；样本量随行返回，不足时如实降级）。"""
    empty = {"length": {"p10": None, "p40": None, "p50": None, "p90": None,
                        "mean": None, "sd": None},
             "medium_probs": None, "medium_counts": {},
             "delay_quantiles": {"p33_min": None, "p66_min": None},
             "initiation_rate": None, "n_messages": 0}

    def _empty_with(meta):
        out = {"version": PROFILE_VERSION, "segment_from": None,
               "phase_id": None, "source": "real_messages", **empty}
        out.update(meta)
        return out

    try:
        ph_now = ph.infer_phase(conn, as_of)
    except sqlite3.Error:
        return _empty_with({"note": "库不可读（demo 空库）→ 优雅降级"})
    seg_from = ph_now.get("valid_from") or "2000-01-01"
    try:
        raw = conn.execute(
            "SELECT day, subtype, content_clean, attachment_name FROM messages "
            "WHERE sender_key='B' AND is_system=0 AND day>=? "
            "AND (TRIM(COALESCE(content_clean,''))<>'' OR "
            "     (attachment_name IS NOT NULL AND attachment_name<>''))",
            (seg_from,)).fetchall()
    except sqlite3.Error:
        return _empty_with({"note": "messages 表不可用（demo 空库）→ 优雅降级"})
    lens, med = [], {"text": 0, "emoji": 0, "image": 0, "voice": 0}
    for _day, subtype, cc, att in raw:
        has_text = bool((cc or "").strip())
        if subtype == "emoji_gif":
            med["emoji"] += 1
        elif (subtype or "").startswith("voice"):
            med["voice"] += 1
        elif att and not has_text:
            med["image"] += 1
        else:
            med["text"] += 1
        if has_text:
            lens.append(len(cc.strip()))
    lens.sort()
    n = len(lens)

    # paired 认领对的 A→B 间隔（复用 feature_series 的桶级 gaps 口径）
    series = ph.feature_series(conn)
    gaps = []
    for s in series:
        if s["bucket"] >= seg_from and s["median_gap_min"] is not None:
            gaps.append(s["median_gap_min"])
    gaps.sort()

    seg_series = [s for s in series if s["bucket"] >= seg_from]
    n_init_days = sum(round((s["initiation_rate"] or 0) * max(1, s["n_a"]))
                      for s in seg_series)
    n_days_total = sum(max(1, s["n_a"]) for s in seg_series)

    mean = sum(lens) / n if n else None
    sd = (sum((x - mean) ** 2 for x in lens) / n) ** 0.5 if n else None

    total_med = sum(med.values())
    return {
        "version": PROFILE_VERSION,
        "segment_from": seg_from,
        "n_messages": n,
        "length": {
            "p10": _quantile(lens, 0.10), "p40": _quantile(lens, 0.40),
            "p50": _quantile(lens, 0.50), "p90": _quantile(lens, 0.90),
            "mean": round(mean, 1) if mean is not None else None,
            "sd": round(sd, 1) if sd is not None else None,      # §13.3-5 方差门禁
        },
        "medium_probs": ({k: round(v / total_med, 4) for k, v in med.items()}
                         if total_med else None),
        "medium_counts": med,
        "delay_quantiles": {"p33_min": _quantile(gaps, 0.33),
                            "p66_min": _quantile(gaps, 0.66)},
        "initiation_rate": round(n_init_days / n_days_total, 4) if n_days_total else None,
        "phase_id": ph_now.get("phase_id"),
        "source": "real_messages",
    }

def length_hint(mode: str, profile: dict) -> str:
    """按行动模式给出长度约束（目标值=真实分位，非手写字数；§11 长度条件化）。"""
    q = (profile or {}).get("length") or {}
    if not q.get("p50"):
        return ""
    if mode == "brief":
        return (f"【长度约束】简短回应：参考真实区间约 {q['p10']}~{q['p40']} 字，"
                f"不要展开长段。")
    if mode == "boundary":
        # 划界属长文表态语域：真实分布 P90 佐证存在长回复，不设短上限
        return (f"【长度约束】立场重申可以完整表达（真实长回复可达 {q['p90']} 字以上），"
                f"不用迁就短消息习惯，但不要重复堆叠。")
    if mode in ("neutral", "engaged"):
        return (f"【长度约束】长度自然起伏（真实中位约 {q['p50']} 字，"
                f"偶有 {q['p90']} 字以上的完整表达），不要每条都差不多长。")
    return ""


def sample_medium(profile: dict, rng, mode: str = "") -> str:
    """按真实媒介分布抽样（§10.6 条件化第一步：阶段级）。

    限制（如实标注）：真实数据没有行动模式×媒介的标注，mode 条件化不做；
    boundary 恒 text 的唯一依据是「划界属语言行为」的设计判断，非数据结论——
    因此也不强加，统一走真实分布。
    """
    probs = (profile or {}).get("medium_probs")
    if not probs:
        return "text"
    roll = rng.random()
    acc = 0.0
    for m in ("text", "emoji", "image", "voice"):
        p = probs.get(m, 0.0)
        acc += p
        if roll < acc:
            return m
    return "text"


def delay_bucket(gap_min: float, profile: dict) -> str:
    """按真实间隔分位分档（P33/P66 为档界，数据出处）。"""
    q = (profile or {}).get("delay_quantiles") or {}
    lo, hi = q.get("p33_min"), q.get("p66_min")
    if gap_min is None:
        return "unknown"
    if lo is not None and gap_min <= lo:
        return "immediate"
    if hi is not None and gap_min <= hi:
        return "within_minutes"
    return "later"


def initiate_check(profile: dict, rng) -> bool:
    """主动开口掷骰：概率=真实 B 先开口天占比（当前段；数据即出处，无手写阈值）。"""
    rate = (profile or {}).get("initiation_rate")
    if rate is None or rate <= 0:
        return False
    return rng.random() <= rate
