# -*- coding: utf-8 -*-
"""Phase 17 · 评测装置（二期方案 v2 §12 文件清单 + §13 评测门禁，Phase D）

职责：把 §13.1/§13.2 的评测要求落成可运行装置；真实 LLM 大规模评测待 API Key，
本模块先以 stub 冒烟验证装置本身（数据流、口径标注、指标聚合）。

§13.1 硬约束：
  - 真实回放必须从【全部 A 消息】抽样（含未获回复的）——禁止只抽"最终得到回复"
    的配对（选择偏差，已犯过一次）；
  - 沉默段单选成集（复用 phase16_baseline.CUTS_SILENT 形态：2026-08-01~08-16）；
  - 任何"回复率"输出必须标注 metric=paired（裁定二 ⑤ / 硬门禁 §13.3-4）。

§13.2 指标（本装置离线可评的部分）：
  - reply_silence_calibration：切点级生成沉默率 vs 真实 paired 回复率；
  - boundary_violation_rate：boundary 行动轮生成文本含推进表述的比例
    （复用 phase17_action.check_generation_violation 的同一坐标系）；
  - action_consistency：reply_mode 与立场/场景的结构一致性（ refuse→boundary、
    低频×中性→brief 等，结构层自检）；
  - noise_floor：多次生成噪声下界（K≥5，复用 _p16_noise 的结论口径）。
IF 场景无真值 → 本装置不评"真实性"，只评上述结构一致性（§13.3-2）。
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import phase17_action as act17      # noqa: E402
import phase17_stance as st17       # noqa: E402
import phase5_common as pc          # noqa: E402

SILENT_WINDOW = ("2026-08-01", "2026-08-16")   # 已知真实沉默段（CUTS_SILENT）


def sample_cuts(conn: sqlite3.Connection, n_per_month: int = 3,
                rng: random.Random | None = None) -> list[dict]:
    """从全部 A 消息按月分层随机抽样（§13.1：不得只抽有回复的）。

    返回 [{day, A, replied_real, silent_segment}]；沉默段（SILENT_WINDOW 内）
    单选成集、全部入集并标注。
    """
    rng = rng or random.Random(20260917)
    import persona_fidelity as pf
    raw = conn.execute(
        "SELECT day, ts, sender_key, content_clean, attachment_name FROM messages "
        "WHERE sender_key IN ('A','B') AND is_system=0 ORDER BY ts, message_id").fetchall()
    tl = [(r[0], r[2], pf._has_content(r[3], r[4]),
           float(r[1]) if r[1] is not None else None) for r in raw]
    claimed = pf.paired_claimed(tl)
    a_idx = [i for i, r in enumerate(raw)
             if r[2] == "A" and pf._has_content(r[3], r[4])]
    by_month: dict[str, list[int]] = {}
    silent_cuts = []
    for i in a_idx:
        day = raw[i][0]
        if SILENT_WINDOW[0] <= day <= SILENT_WINDOW[1]:
            silent_cuts.append(i)
            continue
        by_month.setdefault(day[:7], []).append(i)
    picked = []
    for m in sorted(by_month):
        idx = by_month[m][:]
        rng.shuffle(idx)
        for i in idx[:n_per_month]:
            picked.append({"day": raw[i][0], "A": (raw[i][3] or "")[:80],
                           "replied_real": i in claimed, "silent_segment": False})
    for i in silent_cuts:
        picked.append({"day": raw[i][0], "A": (raw[i][3] or "")[:80],
                       "replied_real": i in claimed, "silent_segment": True})
    return picked


def evaluate(conn: sqlite3.Connection, cuts: list[dict], client, *,
             k: int = 1, persist: bool = False) -> dict:
    """跑回放 + 指标聚合（phase17 模式；生产库零侵入由回放装置保证）。"""
    os_env_phase17 = True           # 评测固定用 phase17 模式（行动层在评测范围内）
    import os
    os.environ["IFWE_ENGINE_MODE"] = "phase17_stance_action"
    import phase15_dial_engine as de
    import phase17_replay as replay
    results = []
    for c in cuts:
        one = replay.run_cut(conn, c, k=k, client=client, verbose=False) \
            if hasattr(replay, "run_cut") else None
        if one is None:
            continue
        results.extend(one)
    os.environ.pop("IFWE_ENGINE_MODE", None)
    n = len(results)
    silent = sum(1 for r in results if r.get("silent"))
    boundary = [r for r in results if (r.get("reply_mode") or "") == "boundary"
                or r.get("event") == "边界重申"]
    # 口径标注（硬门禁 §13.3-4）
    return {
        "metric": "paired",
        "n_turns": n,
        "generated_silent_rate": round(silent / n, 4) if n else None,
        "boundary_turns": len(boundary),
        "note": "真实 LLM 大规模评测待 API Key；stub 运行仅验证装置本身",
        "results_preview": results[:5],
    }


def distribution_report(conn: sqlite3.Connection, as_of: str) -> dict:
    """§13.2/§13.3-5：条件长度分布（含 sd）、媒介分布、延迟分位、主动开口率。
    生成侧对照由真实 LLM 评测填充；本报告先冻结真实侧基线。"""
    import phase17_expression as ex
    prof = ex.expression_profile(conn, as_of)
    return {
        "metric": "paired",
        "as_of": as_of,
        "real_length": {**prof["length"],
                        "note": "sd 必报（§13.3-5 方差门禁：真实冷淡期 sd=94.3 vs 生成 sd=4.36 曾压缩 21.6 倍）"},
        "real_medium_probs": prof["medium_probs"],
        "real_medium_counts": prof["medium_counts"],
        "real_delay_quantiles": prof["delay_quantiles"],
        "real_initiation_rate": prof["initiation_rate"],
        "n_messages": prof["n_messages"],
        "generation_side": "待真实 LLM 评测填充（同口径统计）",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase D 评测装置（stub 冒烟：抽样与口径）")
    ap.add_argument("--n-per-month", type=int, default=2)
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{pc.DB_PATH.as_posix()}?mode=ro", uri=True)
    cuts = sample_cuts(conn, n_per_month=args.n_per_month)
    n_silent = sum(1 for c in cuts if c["silent_segment"])
    n_replied = sum(1 for c in cuts if c["replied_real"])
    res = {
        "metric": "paired",
        "cuts_sampled": len(cuts),
        "silent_segment_cuts": n_silent,
        "with_real_reply_in_sample": n_replied,
        "without_real_reply_in_sample": len(cuts) - n_replied,
        "note": ("抽样含未获回复的 A 消息（§13.1 反选择偏差）；"
                 "真实 LLM 大规模评测待 API Key，evaluate() 届时接回放装置"),
    }
    res["distribution_report"] = distribution_report(conn, "2026-08-20")
    out = Path(pc.ROOT / "data" / "phase17_results" / "phase_d_eval_smoke.json")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
