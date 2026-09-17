# -*- coding: utf-8 -*-
"""IfWe 人格保真模块（Persona Fidelity）v1
—— 把「模拟人格在关系降温期仍热情回复」的失真，从根上修掉

版本（Phase 0 收口 + Phase B 同步，2026-09-17）：
  本文件与私有树 `if_we/pipeline/persona_fidelity.py` **同名同源**；
  唯一结构性差异是配置来源——本树读 `config.py` + config.yaml，
  私有树无 config.py / yaml（v0.1 裁剪决策），用 `_cfg()` 适配层
  （模块常量 + 环境变量覆盖，语义与本树 config 键一一对应）。
  本轮同步：裁定三锚定（分支 P = clip(P_anchor × m)）、§5.2 meta.kind、
  §5.9 sent_at/time_source 配套的时间线注释、n_branch/anchor_day/m 返回字段。

两树统一的**口径升级**（二期方案 v2 §1 裁定二，Phase 0 已落地）：
  「回复率」的权威口径改为 **paired**——
      P(reply) = P( B 在 A 的某一条消息之后 REPLY_WITHIN 内，产出至少一条【有内容的】回复
                    | 该消息由 A 发出 )
  五条硬约束：① 单位是一条 A 消息；② 一一配对（同一条 B 最多认领一条 A，
  认领窗口内最早的未被认领者）；③ silent 标记行既不算 B 的回复、也不算「她说过话」；
  ④ A 在窗口内连发的多条消息各自独立计分，但只有被认领的那条记 1；
  ⑤ 任何对外输出的「回复率」必须标注口径名 `paired`。
  实测：旧口径（任一条 B 落窗即记 1、B 可重复认领）全局 0.833 vs paired 0.661，
  系统偏移 +0.172（见 data/phase16_results/BASELINE_v1.md）。

四件事（对应诊断 R1–R7 的修复 F1–F4；F5 沉默落库由对话内核完成）：

  F1 回复意愿（架构级，最关键）
     「是否回复」不再交给生成模型自由选择（实测提示词授权 150 次生成零选择），
     而是由数据驱动的独立决策产生：
         P(reply) = 过去 W 天内「用户消息被对方回复」的比例（W = 7 与 14 取均值）
     其中「被回复」按 **paired** 口径逐条配对判定（见上）。
     在此基础上做两层**结构性**条件化（均为从数据直接统计的比率，非关键词规则）：
       - partner_recent：对方当天/前一天是否说过话（会话接续 → 回复率显著更高）；
       - streak：对方最后一次说话之后，用户已连发几条未获回应的消息。
     条件层样本不足时逐级回退：精确状态 → 仅 partner_recent → 全局窗口率 → fallback。
     判为不回时**不调用 LLM**（决策与生成分离，同时省成本）。

     ✅ Phase B 已落地（裁定三 §5.1/§5.5）：分支模式下 P = clip(P_anchor × m, 0, 1)，
     P_anchor 只用分支起点前的真实 messages 在 anchor_day 计算一次；分支内 A/B 都
     不进分子或分母（分支 B 行留作 Phase C 的 stance/scene 证据，不作标签）；
     分支起点前无样本 → layer=fallback、P=0.5，不得 0.0。详见
     estimate_reply_probability docstring 的「分支模式」一节。

  F2 真实对话原文窗口
     把最近 N 条真实对话（content_clean，脱敏字段）注入上下文、置于其余上下文之前。
     受隐私门禁（私有树等价 `privacy.allow_llm_send`，环境变量 IFWE_ALLOW_LLM_SEND）；
     IF 分支按分歧点截断，不泄露「被改写掉的真实未来」。

  F3 提示词规则替换
     去掉「绝大多数消息很短 + 1~40 字」双重短约束（与真实降温期长文表态直接冲突），
     改为按情境取长度；同时撤销「不要连续几轮表达同一个意思」的反向诱导——
     划界的本质就是重复重申，要避免的只是逐字复读。

  F4 去重护栏豁免（分位数豁免）
     相似度强制改写护栏会同时拦截「坚定重申立场」与「简短敷衍」。
     豁免判据来自数据：回复长度 ≤ 对方真实消息长度的中位数时豁免（短消息本就高度相似），
     长复读仍保留护栏。

回退：
  - 环境变量 IFWE_PERSONA_FIDELITY=0/false/off/no（**运行期动态读取**）→ 全部关闭，
    行为与改动前完全一致；
  - 环境变量 IFWE_RW_ENABLED=0 → 关闭 F1/F5；IFWE_INJECT_REAL_WINDOW=0 或
    IFWE_ALLOW_LLM_SEND=0 → 关闭 F2；
  - 本模块导入失败时，对话内核回退到原常量与原行为。

多档案：所有统计限定在当前连接的库内（conn 即当前库），绝不跨库查询。
随机性：掷骰使用调用方的 rng（seed 可复现），本模块自身不引入新的随机源。

校准数字（paired 口径，Phase 0 重算；复算脚本 pipeline/phase16_paired_recalc.py，
明细见 data/phase16_results/BASELINE_v1.md 与 paired_recalc.json）：
    全局                旧口径 0.833 → paired 0.645（B 须有内容；偏移 −0.188）
    升温期 2023-04~06   真实 0.572 / E[P] 0.629（偏差 +0.057）
    冲突期 2025-02~04   真实 0.667 / E[P] 0.671（偏差 +0.004）
    冷淡期 2026-08 全段  真实 0.146 / E[P] 0.088（偏差 −0.058）
    ├ 沉默段 08-01~16   真实 0.000 / E[P] 0.049（偏差 +0.049）
    └ 表态段 08-17~23   真实 0.500 / E[P] 0.182（偏差 −0.318，已知残留；
                        分层 partner_recent 已把它从旧版 0.151 抬到 0.182，
                        彻底修复靠 Phase C 的 stance/scene，不靠本模块）
  注：早期复核值「paired 0.661」出自 _review_phase2/_rev_probe4.py，其 B 侧未过滤
  空内容行（7117 条全量）；本实现按方案附录 A 参考实现过滤空内容 B（6635 条），
  0.645 为权威值。旧口径数字一律标注「旧口径·已作废」保留，不静默替换。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta

import config as cfg_mod

# 单一环境变量总开关（运行期每次读取，改动即时生效）
KILL_ENV = "IFWE_PERSONA_FIDELITY"
_KILL_VALUES = {"0", "false", "off", "no"}

# F1 配置缺省（与 config._DEFAULTS 对应；这里再兜一层底，
# 防止旧 config.yaml 缺少新键时 KeyError）
_W_DEFAULTS = {"enabled": True, "windows_days": [7, 14], "fallback": 0.5, "min_samples": 10}


def _cfg() -> dict:
    """配置入口（发布树 = config.load()；私有树同名函数 = 环境变量 + 模块常量）。

    两树业务逻辑一律只经由本函数取配置，保证同名同源可双向核对。
    """
    return cfg_mod.load()


# 发布树用 config.sender_names()（people.A/B.display）。B 视角文案：
# 「我」= 数字人格本人，「对方」= 用户（中性化基线，不用性别词）。


def _db_key(conn: sqlite3.Connection) -> str:
    """库级缓存键（partner_median_len 进程内缓存的失效依据）"""
    return str(cfg_mod.db_path())


def _killed() -> bool:
    return (os.environ.get(KILL_ENV, "") or "").strip().lower() in _KILL_VALUES


def _wcfg() -> dict:
    """F1 配置：config 值覆盖缺省（缺键时用 _W_DEFAULTS 兑底）"""
    raw = (_cfg().get("defaults") or {}).get("reply_willingness") or {}
    out = dict(_W_DEFAULTS)
    if isinstance(raw, dict):
        out.update(raw)
    return out

def f1_enabled() -> bool:
    """F1/F5 回复意愿是否启用"""
    return (not _killed()) and bool(_wcfg().get("enabled"))


def f2_enabled() -> bool:
    """F2 真实原文窗口是否启用（C1：受隐私门禁 allow_llm_send 约束）"""
    if _killed():
        return False
    c = _cfg()
    if not bool(c["defaults"].get("inject_real_window", True)):
        return False
    return bool(c["privacy"].get("allow_llm_send"))


# =============================================================== 口径（裁定二 · paired）

REPLY_WITHIN = 180 * 60      # 秒；A 发言后多久内 B 回算「回了」（与既有 REPLY_WITHIN_MIN=180 一致）


def paired_claimed(rows: list[tuple]) -> set[int]:
    """一一配对（裁定二唯一实现）：返回被认领的 A 行下标集合。

    rows: [(day, sender, has_content, ts), ...] 已按时间正序；
      ts 为对话时间（unix 秒）。真实 messages 行有 ts；分支 sim 行 ts=None
      （created_at 是写入时间，§14 禁止当对话时间；sent_at 落地前分支不参与配对）。
    规则：按时间正序扫 A；每条 A 认领窗口 (a.ts, a.ts+REPLY_WITHIN] 内
    **最早的未被认领**的、有内容的 B；同一条 B 最多认领一条 A。
    """
    b_free = sorted(
        (r[3], i) for i, r in enumerate(rows)
        if r[1] == "B" and r[2] and r[3] is not None)
    used: set[int] = set()
    claimed: set[int] = set()
    for i, r in enumerate(rows):
        if r[1] != "A" or r[3] is None:
            continue
        for bt, j in b_free:
            if j in used:
                continue
            if r[3] < bt <= r[3] + REPLY_WITHIN:
                claimed.add(i)
                used.add(j)
                break
            if bt > r[3] + REPLY_WITHIN:
                break                      # b_free 按时间有序，后面只会更晚
    return claimed


# =============================================================== F1 回复意愿

def _day_diff(a: str, b: str) -> int:
    """b - a 的天数（ISO 日期串；解析失败返回大数，视为不在窗口内）"""
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except Exception:
        return 10 ** 6


def _has_content(content_clean, attachment) -> bool:
    return bool((content_clean or "").strip()) or bool((attachment or "").strip())


def _timeline(conn: sqlite3.Connection, *, sim_id: str, start_day: str,
              current_day: str) -> list[tuple[str, str, bool, float | None]]:
    """合并时间线（按时间正序）：[(day, sender, 是否有实体内容, 对话时间戳|None), ...]

    - 真实历史：messages 表，day < start_day（分支起点之前才可用——
      分支起点之后的时间线属于模拟，真实数据混进来会串味）；ts 取 messages.ts；
    - 分支数据：sim_messages 表，start_day <= day <= current_day。
      ⚠ created_at 是写入时间不是对话时间，排序只认 (day, turn_idx, rowid)，
      ts 恒为 None（§14：不用 created_at 当对话时间）；
      沉默标记行（B 侧空内容）计入时间线但不算「对方说过话」、不参与配对。
    """
    rows: list[tuple[str, str, bool, float | None]] = []
    for day, sk, cc, att, ts in conn.execute(
            "SELECT day, sender_key, content_clean, attachment_name, ts FROM messages "
            "WHERE day < ? AND sender_key IN ('A','B') ORDER BY ts, message_id",
            (start_day,)):
        rows.append((day, sk, _has_content(cc, att), float(ts) if ts is not None else None))
    if sim_id:
        for day, sender, content in conn.execute(
                "SELECT day, sender, content FROM sim_messages "
                "WHERE sim_id=? AND day>=? AND day<=? ORDER BY day, turn_idx, rowid",
                (sim_id, start_day, current_day)):
            rows.append((day, sender, bool((content or "").strip()), None))
    return rows


def _features(rows: list[tuple], idx: int,
              last_active_b: int) -> tuple[int, int]:
    """第 idx 条消息处的结构特征：(partner_recent, streak)

    - partner_recent：对方当天或前一天是否说过话（会话接续的标志）；
    - streak：对方最后一次说话之后，用户已连发几条（封顶 2，只区分 0/1/2+）。
    """
    pr = 0
    if last_active_b >= 0 and _day_diff(rows[last_active_b][0], rows[idx][0]) <= 1:
        pr = 1
    streak = sum(1 for j in range(last_active_b + 1, idx) if rows[j][1] == "A")
    return pr, min(streak, 2)


def _rate(sel: list[dict]) -> tuple[float | None, int]:
    n = len(sel)
    if not n:
        return None, 0
    return sum(s["replied"] for s in sel) / n, n


def estimate_reply_probability(conn: sqlite3.Connection, *, sim_id: str = "",
                               start_day: str, current_day: str,
                               divergence_point: str | None = None,
                               m: float = 1.0) -> dict:
    """F1：估计「对方回复用户当前这条消息」的概率 P ∈ [0,1]。

    两种模式（同一份实现，行为按是否有 sim_id 分流）：

    分支模式（sim_id 非空；产品路径 · 裁定三锚定）：
      P = clip(P_anchor × m, 0, 1)，初版 m 恒 1.0（Phase C 起由 stance/scene 提供）。
      - P_anchor 只用分支起点前的真实 messages，在 anchor_day（=start_day）计算一次；
        真实历史不可变 → 重算确定性等于冻结（不随分支推进漂移，§5.1/§5.5）；
      - 分支内 A（用户输入）与 B（模拟输出）都**不进入** P 的分子或分母；
        分支 B 行可作 stance/scene 证据，但不是标签（Phase C 接线）；
      - 「当前待预测消息」= 在 anchor_day 追加的假想用户消息（真实末条 A 是历史样本，
        不排除——与实验路径的挂起排除是两种口径，Phase A 观测已归因）；
      - 分支起点前无真实样本 → layer=fallback、P=fallback(0.5)，**不得为 0.0**（§5.5）；
      - 返回额外携带 anchor_day / m / n_samples（落库 sim_decision_log 对应列）。

    实验模式（sim_id 为空；回放/校准脚本）：
      滚动窗口到 current_day；时间线最后一条若是用户消息，视为「当前待预测消息」
      不进入样本（与 Phase 0 基线口径一致，勿混用两种模式的数字）。

    返回 {"p", "layer", "state", "samples", "rate7", "rate14", "metric",
          "n_branch", "anchor_day", "m", "n_samples"}；
    layer ∈ exact / partner_recent / global / fallback（fallback = 样本不足的中性值）；
    metric 恒为 "paired"（裁定二第 ⑤ 条：对外输出必须标注口径名）。
    divergence_point 预留：F1 的真实历史截断以 start_day 为准（分支期真实数据不混入，
    与 F2 的分歧点截断是两回事）。
    """
    w = _wcfg()
    try:
        windows = [int(x) for x in w.get("windows_days") or [7, 14] if int(x) > 0]
    except Exception:
        windows = [7, 14]
    if not windows:
        windows = [7, 14]
    min_n = max(1, int(w.get("min_samples", 10)))

    branch_mode = bool(sim_id)
    if branch_mode:
        # 锚定：只用真实历史；anchor_day = 分支起点；current_day 不参与（防漂移）
        anchor_day = start_day
        rows = _timeline(conn, sim_id="", start_day=start_day,
                         current_day=anchor_day)
    else:
        anchor_day = None
        rows = _timeline(conn, sim_id="", start_day=start_day,
                         current_day=current_day)
    claimed = paired_claimed(rows)     # paired 口径：真实段一一配对，算一次

    # 逐条扫样本：每条用户消息的 (特征, 是否被回复)
    samples: list[dict] = []
    state = {"partner_recent": 0, "streak": 0}
    last_active_b = -1
    n = len(rows)
    n_branch = 0                     # 分支段样本数（Phase A 自反馈检测输入，纯增量字段）
    for i, (day, sender, active, ts) in enumerate(rows):
        if sender == "B":
            if active:                 # silent 行不算「她说过话」（裁定二第 ③ 条）
                last_active_b = i
            continue
        pr, streak = _features(rows, i, last_active_b)
        if (not branch_mode) and i == n - 1 and sender == "A":
            state = {"partner_recent": pr, "streak": streak}   # 当前待预测消息（仅实验模式）
            continue
        if ts is not None:
            # 真实段：paired 一一配对标签（180 分钟、B 有内容、一条 B 只认领一条 A）
            replied = 1 if i in claimed else 0
        else:
            # 分支段标签：裁定三后 P 不再使用分支段（branch_mode 时间线不含 sim 行）。
            # 此分支只在「实验模式 + 混合时间线」的旧式调用里可达，保留以兼容诊断工具；
            # 其 day 粒度近似语义见模块 docstring ⚠️（Phase B 锚定后产品路径不走这里）。
            replied = 1 if any(rows[j][1] == "B" and rows[j][2] and rows[j][0] == day
                               for j in range(i + 1, n)) else 0
            n_branch += 1
        samples.append({"day": day, "pr": pr, "streak": streak, "replied": replied})
    if branch_mode or not (rows and rows[-1][1] == "A"):
        # 分支模式：当前待预测消息 = anchor_day 的假想用户消息（真实末条 A 是历史样本）；
        # 实验模式独立调用：按「此刻在 current_day 追加一条用户消息」估计
        pend_day = anchor_day or current_day
        state = dict(zip(("partner_recent", "streak"),
                         _features(rows + [(pend_day, "A", True, None)], len(rows),
                                   last_active_b)))

    ref_day = anchor_day or current_day      # 窗口参照日：分支模式恒为 anchor_day（锚定）
    recent = [s for s in samples if 0 <= _day_diff(s["day"], ref_day) < max(windows)]

    # 条件层（结构性特征，样本充足才启用）
    pool = recent
    sl = state["streak"]
    r_exact, n_exact = _rate([s for s in pool
                              if s["pr"] == state["partner_recent"] and s["streak"] == sl])
    if n_exact >= min_n:
        p, layer = r_exact, "exact"
    else:
        r_pr, n_pr = _rate([s for s in pool if s["pr"] == state["partner_recent"]])
        if n_pr >= min_n:
            p, layer = r_pr, "partner_recent"
        else:
            p, layer = None, ""

    # 全局层：W 窗口率取均值（文档 F1 原口径，标签已按 paired 计）
    rates = []
    for k in windows:
        r, nn = _rate([s for s in samples if 0 <= _day_diff(s["day"], ref_day) < k])
        if nn:
            rates.append(r)
    if p is None:
        if rates:
            p = sum(rates) / len(rates)
            layer = "global"
        else:
            p = float(w.get("fallback", 0.5))
            layer = "fallback"

    p = round(min(1.0, max(0.0, float(p))), 4)
    # 裁定三调制：P = clip(P_anchor × m)。初版 m 恒 1.0（Phase C 由 stance/scene 提供，
    # 每档须有真实数据出处 + 留出验证，禁止手写阈值）。
    m_eff = min(1.0, max(0.0, float(m)))
    p = round(min(1.0, max(0.0, p * m_eff)), 4)
    r7, _ = _rate([s for s in samples if 0 <= _day_diff(s["day"], ref_day) < windows[0]])
    return {"p": p, "layer": layer, "state": state, "samples": len(recent),
            "rate7": r7, "rate14": (rates[-1] if rates else None), "metric": "paired",
            "n_branch": n_branch, "anchor_day": anchor_day, "m": m_eff,
            "n_samples": len(samples)}


def paired_window_rate(conn: sqlite3.Connection, day: str, window: int,
                       sim_id: str | None = None) -> tuple[float | None, int]:
    """paired 口径的局部回复率（唯一实现；供兼容垫片 `_local_rate` 与校准脚本调用）。

    返回 (rate|None, n_a)。窗口 [day-window, day)：
      - 真实段 [lo, min(day, branch_start))：SQL 取 ts 后按裁定二一一配对；
        B 侧必须有实体内容（silent/空内容不算回复）。
      - 分支段 [max(lo, branch_start), day)：day 粒度近似 + silent 排除
        （分支标签问题由 Phase B 锚定根除，见模块 docstring ⚠️）。
    """
    try:
        lo = (date.fromisoformat(day) - timedelta(days=int(window))).isoformat()
    except Exception:
        return None, 0
    branch_start = None
    if sim_id:
        row = conn.execute("SELECT start_day FROM sim_runs WHERE sim_id=?",
                           (sim_id,)).fetchone()
        branch_start = row[0] if row else None

    hi_real = min(day, branch_start) if branch_start else day
    n_a = n_rep = 0
    if lo < hi_real:
        a_rows = conn.execute(
            "SELECT ts FROM messages WHERE sender_key='A' AND day >= ? AND day < ? "
            "AND is_system=0 AND ts IS NOT NULL ORDER BY ts", (lo, hi_real)).fetchall()
        b_ts = sorted(float(r[0]) for r in conn.execute(
            "SELECT ts FROM messages WHERE sender_key='B' AND day >= ? AND day < ? "
            "AND is_system=0 AND ts IS NOT NULL "
            "AND TRIM(COALESCE(content_clean,'')) <> ''", (lo, hi_real)))
        a_ts = [float(r[0]) for r in a_rows]
        n_a = len(a_ts)
        n_rep = 0
        used: set[int] = set()
        bj = 0                          # b_ts / a_ts 均按时间有序 → 双指针一一配对
        for at in a_ts:
            while bj < len(b_ts) and b_ts[bj] <= at:
                bj += 1                 # 早于等于该 A 的 B 永远进不了窗口（对后续 A 更不可能）
            k = bj
            while k < len(b_ts) and b_ts[k] <= at + REPLY_WITHIN:
                if k not in used:       # 认领窗口内最早的未被认领者（裁定二第 ② 条）
                    used.add(k)
                    n_rep += 1
                    break
                k += 1
    # 分支段（day 粒度近似；silent 行不算）
    if branch_start and branch_start < day:
        lo2 = max(lo, branch_start)
        for _d, na, nb in conn.execute(
                "SELECT day, SUM(sender='A'), "
                "SUM(sender='B' AND TRIM(COALESCE(content,'')) <> '') FROM sim_messages "
                "WHERE sim_id=? AND day >= ? AND day < ? GROUP BY day",
                (sim_id, lo2, day)).fetchall():
            na = na or 0
            n_a += na
            if nb:
                n_rep += na
    if not n_a:
        return None, 0
    return n_rep / n_a, n_a


# =============================================================== F2 真实原文窗口

def real_window_text(conn: sqlite3.Connection, *, start_day: str,
                     divergence_point: str | None = None,
                     has_rewrite: bool = False, limit: int | None = None) -> str:
    """最近 N 条真实对话（content_clean，脱敏字段）；返回注入文本（禁用/无数据时为空串）。

    - IF 分支（has_rewrite=True）按分歧点截断，避免泄露被改写掉的真实未来；
    - C1：allow_llm_send=false 时整体禁用（见 f2_enabled）。
    """
    if not f2_enabled():
        return ""
    if limit is None:
        limit = int(_cfg()["defaults"].get("window_turns", 20))
    limit = max(1, min(100, limit))
    cut = divergence_point if (has_rewrite and divergence_point) else start_day
    try:
        rows = conn.execute(
            "SELECT day, sender_key, content_clean FROM messages "
            "WHERE day < ? AND sender_key IN ('A','B') "
            "AND TRIM(COALESCE(content_clean,'')) <> '' "
            "ORDER BY ts DESC, message_id DESC LIMIT ?", (cut, limit)).fetchall()
    except sqlite3.Error:
        return ""
    rows = list(reversed(rows))
    if not rows:
        return ""
    names = cfg_mod.sender_names()
    lines = [f"[{d} {names.get(sk, sk)}] {(cc or '').strip()}" for d, sk, cc in rows]
    return ("【最近的真实对话（对方当时的真实语气与状态，供参考；此后为本时间线）】\n"
            + "\n".join(lines))


# =============================================================== F3 提示词规则

DIAL_RULES_V2 = """
【聊天场景（产品态，优先于上面的一般规则）】
你正在用微信和对方聊天，只输出你**实际会发出的那条消息**，不要旁白、不要分析、不要总结。
- 长度按情境自然取用：日常闲聊就短（几个字到十几个字）；但当你确实需要认真表达立场、
  解释或表态时，可以写一条完整的长消息——真实的表达本就长短悬殊。
- 允许只回一两个字、允许答非所问、允许用语气词收尾（的/吗/吧/呢/嗯/啊），允许突然岔开话题。
- 不要每条都关心对方、不要每条都推进关系、不要复述或概括对方刚说的话、不要分点、不要排比。
- 如果你的立场没有变化，重复之前的态度是真实且允许的（划界本就是反复重申）；
  要避免的只是逐字复读同一条消息。
- 不要提及你不可能知道的未来信息，也不要引用档案之外的经历。
- action 只在确有并列动作时填写（例如「隔了很久才回」）；没有就留空串。
"""

# phase5_a2_loop.REPLY_RULES 的替换版：只动第 2 条（长度）与第 6 条（重复），
# 其余规则保持原文——它们是两仓库共有的领域知识坐标系。
REPLY_RULES_V2 = """规则：
1. 只依据「当前世界/记忆/对话缓冲」与你的档案行动；严禁捏造档案之外的经历细节。
2. 做自己，不要做“复读机/客服”：
   - 不要逐句回应对方的所有内容；可以用半截话、口头禅、突然岔开、不完全回答。
   - 不要机械复述对方的用词；不要每回合都谈关系/推进关系。
   - 消息长度按情境自然起伏（闲聊短，认真表态可以长），偶尔来一句完整的长话；少用排比/对称句。
3. 媒介自然混用：大部分是文字，但心情/日常时可以不定期发图、表情包或语音（medium 选 image/emoji/voice），别永远是一段段成句文字。
4. 8~10 条以内的消息流承上一句即可，允许出现“没接住话”“换个话头”的真实感。
5. 情绪(emotion)与行动(action)需符合你的 S 层模式与当前处境；action 为可选的并列动作，无则空串。
6. **不要机械重复前几轮已聊过的话题/行动**（例如连续多日点同一家外卖）——日期在推进，每次互动应带来新的信息、情绪或关系进展（哪怕很小）；但立场的重申不算机械重复——态度未变时，坚定重复它是真实的。
7. **改写感知**：若世界状态中出现了【最重要·改写决定】或【最重要·改写已生效】标记，你必须把这个改写视为事实，并把互动**从回应这个决定开始**（可提及、可行动），但不要反复念叨同一句话。
8. 完成一次自然的关系互动即可，不要替对方做决定。"""


def dial_rules(original: str) -> str:
    """对话内核取规则用：开关关闭时原样返回（行为与改动前一致）"""
    return original if _killed() else DIAL_RULES_V2


# =============================================================== F4 去重护栏豁免

_MEDIAN_CACHE: dict[str, int | None] = {}


def partner_median_len(conn: sqlite3.Connection) -> int | None:
    """对方真实消息的长度中位数（F4 豁免阈值，数据推导、无拍脑袋常数）。

    按库路径做进程内缓存；换库自动失效。无数据返回 None（不豁免）。
    """
    key = _db_key(conn)
    if key in _MEDIAN_CACHE:
        return _MEDIAN_CACHE[key]
    try:
        lens = sorted(len(r[0].strip()) for r in conn.execute(
            "SELECT content_clean FROM messages "
            "WHERE sender_key='B' AND TRIM(COALESCE(content_clean,'')) <> ''"))
    except sqlite3.Error:
        lens = []
    v = lens[len(lens) // 2] if lens else None
    _MEDIAN_CACHE[key] = v
    return v


def apply(a2loop) -> None:
    """模块级 patch（由对话内核在常量定义之后调用）。

    - 用 F3 版替换 a2loop.REPLY_RULES（⚠ 该名字在 a2loop 内是 from-import 绑定，
      必须 patch **使用它的那个模块** 的属性，patch phase5_llm 源模块不生效）；
    - 包装 PersonaAgent._recent_similarity：短回复（≤ 对方真实长度中位数）豁免。
      wrapper 内部动态检查总开关，因此环境变量改动即时生效。
    幂等：重复调用不重复 patch。
    """
    if getattr(a2loop, "_pf_patched", False):
        return
    orig_sim = a2loop.PersonaAgent._recent_similarity

    def _similarity_with_exempt(self, text: str) -> float:
        if not _killed():
            med = _EXEMPT_LEN["value"]
            if med is not None and len((text or "").strip()) <= med:
                return 0.0
        return orig_sim(self, text)

    a2loop.PersonaAgent._recent_similarity = _similarity_with_exempt
    if not _killed():
        a2loop.REPLY_RULES = REPLY_RULES_V2
    a2loop._pf_patched = True
    a2loop._pf_orig_similarity = orig_sim


# F4 豁免阈值（对方真实消息长度中位数）。PersonaAgent 不持有 conn，
# 由对话内核在初始化时注册；未注册（如直接跑 phase5 CLI）则不豁免。
_EXEMPT_LEN: dict = {"value": None}


def register_exempt_len(median_len: int | None) -> None:
    _EXEMPT_LEN["value"] = median_len
