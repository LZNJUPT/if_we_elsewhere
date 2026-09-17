# -*- coding: utf-8 -*-
"""Phase 17 · 关系阶段 / 变化点模型（二期方案 v2 §6.1B + §7.2，Phase D）

候选模型递进（§7.2）：① 多时间尺度统计 + 数据学习权重 → ② 变化点检测。
本模块实现 ①+②：paired 口径特征序列（§1 裁定二）+ 二分分割变化点检测
（置换检验显著性，无手写阈值）+ 段级特征快照。

硬约束：
  - 全部统计走 persona_fidelity.paired_claimed（paired 口径唯一实现）；
  - 变化点显著性用置换检验（打乱序列看增益分布），min_seg 是结构参数（约一个月），
    不是行为阈值；未做留出验证前，本模块**只观测不决策**（不接进引擎行为——
    m 调制档位须待验证通过，裁定三）；
  - 阶段标签不映射成语义名（避免手写阈值）：输出段内特征的相对分位快照，
    语义判断由立场层（phase17_stance）的证据链承担；
  - 分支内不使用模型自身的 B 回复数更新阶段（§7.2）——本模块只读真实 messages。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

import persona_fidelity as pf

PHASE_VERSION = "phase_mvp_v1"

MIN_SEG = 4              # 最小段长（桶数）；结构参数：约 4 周，防止碎片段
N_PERM = 300             # 置换检验次数（数据驱动显著性，替代手写增益阈值）
PERM_P = 0.01            # 显著性水平。取 0.01 而非 0.05 的出处：二分分割递归会做
                         # 2~4 次检验（多重比较），Bonferroni 校正 0.05/3≈0.017，
                         # 取整到 0.01（实测：0.05 下纯噪声序列出现边缘误报 p=0.04）


def _bucket_start(day: str, bucket_days: int) -> str:
    d = date.fromisoformat(day)
    epoch = date(2022, 1, 3)                     # 固定纪元周一（仅对齐用，非业务日期）
    idx = (d - epoch).days // bucket_days
    return (epoch + timedelta(days=idx * bucket_days)).isoformat()


def feature_series(conn: sqlite3.Connection, *, bucket_days: int = 7,
                   start_day: str | None = None,
                   end_day: str | None = None) -> list[dict]:
    """按时间桶聚合 §7.2 特征（全部真实 messages，paired 口径）。

    返回按桶升序：[{bucket, n_a, n_b, reply_rate(paired), initiation_rate,
                    median_gap_min, len_b_mean, len_b_sd}, ...]
    - reply_rate：桶内 A 消息被认领比例（paired 一一配对，180 分钟、B 有内容）；
    - initiation_rate：当天首条消息由 B 发出的天数占比（B 主动开口率）；
    - len_b_mean/sd：B 消息长度均值与标准差（§13.2 方差门禁）；
    - median_gap_min：被认领 A→其窗口内最早有内容 B 的中位间隔（分钟；
      与认领对象一致的近似口径——认领取窗口内最早未认领者，二者在低并发下重合）。
    """
    raw = conn.execute(
        "SELECT day, ts, sender_key, content_clean, attachment_name FROM messages "
        "WHERE sender_key IN ('A','B') AND is_system=0 ORDER BY ts, message_id").fetchall()
    if start_day:
        raw = [r for r in raw if r[0] >= start_day]
    if end_day:
        raw = [r for r in raw if r[0] <= end_day]
    if not raw:
        return []
    tl = [(r[0], r[2], pf._has_content(r[3], r[4]), float(r[1]) if r[1] is not None else None)
          for r in raw]
    claimed = pf.paired_claimed(tl)

    buckets: dict[str, dict] = {}
    for i, (day, sender, active, ts) in enumerate(tl):
        b = _bucket_start(day, bucket_days)
        bk = buckets.setdefault(b, {"n_a": 0, "n_b": 0, "b_lens": [], "gaps": [],
                                    "claimed_here": 0, "day_first": {}})
        if sender == "A" and active:
            bk["n_a"] += 1
            if i in claimed:
                bk["claimed_here"] += 1
                bj = next((j for j in range(i + 1, len(tl))
                           if tl[j][1] == "B" and tl[j][2] and tl[j][3] is not None
                           and tl[j][3] > ts and tl[j][3] <= ts + pf.REPLY_WITHIN), None)
                if bj is not None:
                    bk["gaps"].append((tl[bj][3] - ts) / 60.0)
        elif sender == "B" and active:
            bk["n_b"] += 1
            cc = next(r[3] for r in raw
                      if r[0] == day and r[2] == "B" and r[1] == ts)
            bk["b_lens"].append(len((cc or "").strip()))
        # 每天首条发送者（B 先开口 = initiation）
        fd = bk["day_first"].get(day)
        if fd is None or (ts is not None and (fd[0] is None or ts < fd[0])):
            bk["day_first"][day] = (ts, sender)

    out = []
    for b in sorted(buckets):
        bk = buckets[b]
        n_a = bk["n_a"]
        days = list(bk["day_first"])
        n_init = sum(1 for d in days if bk["day_first"][d][1] == "B")
        lens = bk["b_lens"]
        mean = sum(lens) / len(lens) if lens else None
        sd = (sum((x - mean) ** 2 for x in lens) / len(lens)) ** 0.5 if lens else None
        gaps = sorted(bk["gaps"])
        out.append({
            "bucket": b,
            "n_a": n_a, "n_b": bk["n_b"],
            "reply_rate": (bk["claimed_here"] / n_a) if n_a else None,
            "initiation_rate": (n_init / len(days)) if days else None,
            "median_gap_min": gaps[len(gaps) // 2] if gaps else None,
            "len_b_mean": round(mean, 1) if mean is not None else None,
            "len_b_sd": round(sd, 1) if sd is not None else None,
        })
    return out


# ---------------------------------------------------------------- 变化点检测
def _seg_cost(series: list[float], lo: int, hi: int) -> float:
    """段内平方误差和（lo 含、hi 不含）"""
    seg = [x for x in series[lo:hi] if x is not None]
    n = len(seg)
    if n < 2:
        return 0.0
    m = sum(seg) / n
    return sum((x - m) ** 2 for x in seg)


def detect_changepoints(series: list[float], *, min_seg: int = MIN_SEG,
                        n_perm: int = N_PERM, p_threshold: float = PERM_P,
                        rng: random.Random | None = None) -> list[dict]:
    """二分分割变化点检测（对 reply_rate 序列；None 视为缺失跳过）。

    显著性 = 置换检验：打乱序列后同样分割的增益分布 → 经验 p 值。
    返回 [{index, gain, p_value}, ...]（index 为序列下标，即新段起点）。
    递归分割直到无显著点或段太短。
    """
    rng = rng or random.Random(20260917)
    valid = [i for i, x in enumerate(series) if x is not None]
    if len(valid) < min_seg * 2:
        return []
    cps: list[dict] = []

    def _perm_gain(lo: int, hi: int, split: int) -> float:
        left = series[lo:split]
        right = series[split:hi]
        full = left + right
        base = _seg_cost_list(full)
        return (base - _seg_cost_list(left) - _seg_cost_list(right))

    def _seg_cost_list(seg: list[float]) -> float:
        vv = [x for x in seg if x is not None]
        n = len(vv)
        if n < 2:
            return 0.0
        m = sum(vv) / n
        return sum((x - m) ** 2 for x in vv)

    def _split(lo: int, hi: int) -> None:
        if hi - lo < min_seg * 2:
            return
        best_i, best_gain = None, 0.0
        for s in range(lo + min_seg, hi - min_seg + 1):
            left = [x for x in series[lo:s] if x is not None]
            right = [x for x in series[s:hi] if x is not None]
            if len(left) < 2 or len(right) < 2:
                continue
            gain = (_seg_cost_list([x for x in series[lo:hi] if x is not None])
                    - _seg_cost_list(left) - _seg_cost_list(right))
            if gain > best_gain:
                best_i, best_gain = s, gain
        if best_i is None or best_gain <= 0:
            return
        # 置换检验：段内随机重排的增益分布
        seg_vals = [x for x in series[lo:hi] if x is not None]
        ge = 0
        for _ in range(n_perm):
            shuffled = seg_vals[:]
            rng.shuffle(shuffled)
            s_local = best_i - lo
            g = (_seg_cost_list(shuffled)
                 - _seg_cost_list(shuffled[:s_local])
                 - _seg_cost_list(shuffled[s_local:]))
            if g >= best_gain:
                ge += 1
        p = ge / n_perm
        if p < p_threshold:
            cps.append({"index": best_i, "gain": round(best_gain, 4),
                        "p_value": round(p, 4)})
            _split(lo, best_i)
            _split(best_i, hi)

    _split(0, len(series))
    return sorted(cps, key=lambda c: c["index"])


def infer_phase(conn: sqlite3.Connection, as_of: str, *, bucket_days: int = 7) -> dict:
    """as_of 所在的阶段快照（§6.1B 结构；全真实数据，分支无关）。

    返回 {phase_id, version, features, percentile, valid_from, valid_to,
          changepoints, bucket_days, feature_snapshot}；
    percentile = 段特征在全序列历史中的分位（相对定义，非绝对阈值）。
    注意：全量重算 O(N)；真实库 ~15k 行、周桶 ~200 个，秒级，可接受。
    """
    series = feature_series(conn, bucket_days=bucket_days)
    if not series:
        return {"phase_id": None, "version": PHASE_VERSION, "features": None,
                "note": "无真实数据"}
    rates = [s["reply_rate"] for s in series]
    cps = detect_changepoints(rates)
    cuts = [0] + [c["index"] for c in cps] + [len(series)]
    seg_idx = None
    for k in range(len(cuts) - 1):
        if series[cuts[k]]["bucket"] <= as_of[:10] <= series[cuts[k + 1] - 1]["bucket"] \
                or (cuts[k] <= len(series) - 1 and as_of[:10] >= series[cuts[k]]["bucket"]
                    and (k == len(cuts) - 2 or as_of[:10] < series[cuts[k + 1]]["bucket"])):
            seg_idx = k
            break
    if seg_idx is None:
        seg_idx = len(cuts) - 2
    seg = series[cuts[seg_idx]:cuts[seg_idx + 1]]
    rates_seg = [s["reply_rate"] for s in seg if s["reply_rate"] is not None]
    feats = {}
    for key in ("reply_rate", "initiation_rate", "len_b_sd"):
        vals = [s[key] for s in series if s[key] is not None]
        v = [s[key] for s in seg if s[key] is not None]
        if vals and v:
            seg_m = sum(v) / len(v)
            feats[key] = round(seg_m, 4)
            feats[key + "_pct"] = round(sum(1 for x in vals if x < seg_m) / len(vals), 3)
    return {
        "phase_id": f"seg{seg_idx}",
        "version": PHASE_VERSION,
        "features": feats,
        "percentile": {k: feats.get(k + "_pct") for k in
                       ("reply_rate", "initiation_rate", "len_b_sd")},
        "valid_from": seg[0]["bucket"],
        "valid_to": seg[-1]["bucket"],
        "changepoints": cps,
        "bucket_days": bucket_days,
        "n_buckets": len(series),
        "feature_snapshot": seg[-1],
        "source": "real_messages_paired",
    }
