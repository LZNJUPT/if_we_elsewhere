# -*- coding: utf-8 -*-
"""
IfWe 人格保真模块（Persona Fidelity）v1
—— 把「模拟人格在关系降温期仍热情回复」的失真，从根上修掉

四件事（对应诊断 R1–R7 的修复 F1–F4；F5 沉默落库由对话内核完成）：

  F1 回复意愿（架构级，最关键）
     「是否回复」不再交给生成模型自由选择（实测提示词授权 150 次生成零选择），
     而是由数据驱动的独立决策产生：
         P(reply) = 过去 W 天内「用户消息被对方回复」的比例（W = 7 与 14 取均值）
     在此基础上做两层**结构性**条件化（均为从数据直接统计的比率，非关键词规则）：
       - partner_recent：对方当天/前一天是否说过话（会话接续 → 回复率显著更高）。
         直接修复已知残留：「对方刚表态后仍在收尾回应的那几天」被全局率低估的问题；
       - streak：对方最后一次说话之后，用户已连发几条未获回应的消息
         （连续单方面喊话 → 回复率进一步走低）。
     条件层样本不足时逐级回退：精确状态 → 仅 partner_recent → 全局窗口率 → fallback。
     判为不回时**不调用 LLM**（决策与生成分离，同时省成本）。

  F2 真实对话原文窗口
     把最近 N 条真实对话（content_clean，脱敏字段）注入上下文、置于其余上下文之前。
     受 privacy.allow_llm_send 门禁（用户未授权外发时自动禁用）；IF 分支按分歧点截断，
     不泄露「被改写掉的真实未来」。

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
  - defaults.reply_willingness.enabled=false → 关闭 F1/F5；
  - defaults.inject_real_window=false 或 privacy.allow_llm_send=false → 关闭 F2；
  - 本模块导入失败时，对话内核回退到原常量与原行为。

多档案：所有统计限定在当前档案的数据目录内（conn 即当前库），绝不跨档案查询。
随机性：掷骰使用 world.clock.rng（seed 可复现），本模块自身不引入新的随机源。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date

import config as cfg_mod

# 单一环境变量总开关（运行期每次读取，改动即时生效）
KILL_ENV = "IFWE_PERSONA_FIDELITY"
_KILL_VALUES = {"0", "false", "off", "no"}

# F1 配置缺省（与 config._DEFAULTS 对应；这里再兜一层底，
# 防止旧 config.yaml 缺少新键时 KeyError）
_W_DEFAULTS = {"enabled": True, "windows_days": [7, 14], "fallback": 0.5, "min_samples": 10}


def _killed() -> bool:
    return (os.environ.get(KILL_ENV, "") or "").strip().lower() in _KILL_VALUES


def _wcfg() -> dict:
    raw = (cfg_mod.load().get("defaults") or {}).get("reply_willingness") or {}
    out = dict(_W_DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if v is not None})
    return out


def f1_enabled() -> bool:
    """F1/F5 回复意愿是否启用"""
    return (not _killed()) and bool(_wcfg().get("enabled"))


def f2_enabled() -> bool:
    """F2 真实原文窗口是否启用（C1：受 privacy.allow_llm_send 门禁）"""
    if _killed():
        return False
    d = cfg_mod.load().get("defaults") or {}
    if not bool(d.get("inject_real_window", True)):
        return False
    return bool(cfg_mod.load().get("privacy", {}).get("allow_llm_send"))


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
              current_day: str) -> list[tuple[str, str, bool]]:
    """合并时间线（按时间正序）：[(day, sender, 是否有实体内容), ...]

    - 真实历史：messages 表，day < start_day（分支起点之前才可用——
      分支起点之后的时间线属于模拟，真实数据混进来会串味）；
    - 分支数据：sim_messages 表，start_day <= day <= current_day。
      ⚠ created_at 是写入时间不是对话时间，排序只认 (day, turn_idx)；
      沉默标记行（B 侧空内容）计入时间线但不算「对方说过话」。
    """
    rows: list[tuple[str, str, bool]] = []
    for day, sk, cc, att in conn.execute(
            "SELECT day, sender_key, content_clean, attachment_name FROM messages "
            "WHERE day < ? AND sender_key IN ('A','B') ORDER BY ts, message_id",
            (start_day,)):
        rows.append((day, sk, _has_content(cc, att)))
    if sim_id:
        for day, sender, content in conn.execute(
                "SELECT day, sender, content FROM sim_messages "
                "WHERE sim_id=? AND day>=? AND day<=? ORDER BY day, turn_idx, rowid",
                (sim_id, start_day, current_day)):
            rows.append((day, sender, bool((content or "").strip())))
    return rows


def _features(rows: list[tuple[str, str, bool]], idx: int,
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
                               divergence_point: str | None = None) -> dict:
    """F1：估计「对方回复用户当前这条消息」的概率 P ∈ [0,1]。

    返回 {"p", "layer", "state", "samples", "rate7", "rate14"}；
    layer ∈ exact / partner_recent / global / fallback（fallback = 样本不足的中性值）。
    时间线上最后一条若是用户消息，视为「当前待预测消息」，不进入样本。
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

    rows = _timeline(conn, sim_id=sim_id, start_day=start_day, current_day=current_day)

    # 逐条扫样本：每条用户消息的 (特征, 当天是否被对方回过)
    samples: list[dict] = []
    state = {"partner_recent": 0, "streak": 0}
    last_active_b = -1
    n = len(rows)
    for i, (day, sender, active) in enumerate(rows):
        if sender == "B":
            if active:
                last_active_b = i
            continue
        pr, streak = _features(rows, i, last_active_b)
        if i == n - 1 and sender == "A":
            state = {"partner_recent": pr, "streak": streak}   # 当前待预测消息
            continue
        replied = 1 if any(rows[j][1] == "B" and rows[j][2] and rows[j][0] == day
                           for j in range(i + 1, n)) else 0
        samples.append({"day": day, "pr": pr, "streak": streak, "replied": replied})
    if not (rows and rows[-1][1] == "A"):
        # 没有挂起的用户消息（独立调用）：按「此刻在 current_day 追加一条用户消息」估计
        state = dict(zip(("partner_recent", "streak"),
                         _features(rows + [(current_day, "A", True)], len(rows),
                                   last_active_b)))

    recent = [s for s in samples if 0 <= _day_diff(s["day"], current_day) < max(windows)]

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

    # 全局层：W 窗口率取均值（文档 F1 原口径）
    rates = []
    for k in windows:
        r, nn = _rate([s for s in samples if 0 <= _day_diff(s["day"], current_day) < k])
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
    r7, _ = _rate([s for s in samples if 0 <= _day_diff(s["day"], current_day) < windows[0]])
    return {"p": p, "layer": layer, "state": state, "samples": len(recent),
            "rate7": r7, "rate14": (rates[-1] if rates else None)}


# =============================================================== F2 真实原文窗口

def real_window_text(conn: sqlite3.Connection, *, start_day: str,
                     divergence_point: str | None = None,
                     has_rewrite: bool = False, limit: int | None = None) -> str:
    """最近 N 条真实对话（content_clean，脱敏字段）；返回注入文本（禁用/无数据时为空串）。

    - IF 分支（has_rewrite=True）按分歧点截断，避免泄露被改写掉的真实未来；
    - C1：privacy.allow_llm_send=false 时整体禁用（见 f2_enabled）。
    """
    if not f2_enabled():
        return ""
    if limit is None:
        limit = int((cfg_mod.load().get("defaults") or {}).get("window_turns", 20))
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

    按库路径做进程内缓存；换档案/换库自动失效。无数据返回 None（不豁免）。
    """
    key = str(cfg_mod.db_path())
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
