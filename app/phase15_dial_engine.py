# -*- coding: utf-8 -*-
"""
IfWe Phase 15 · 对话内核（Dial Engine）v1
—— 把「看两个 AI 演戏 + 问分析问题」改成「你自己上场和对方对话」

与 Phase 5~14 的根本差别（两处）：
  D-1 谁说话：A 不再由 LLM 扮演 —— 由【用户本人】逐条输入（本模块不再实例化 A 的 PersonaAgent）
  D-2 谁推进时间：不再「按天循环 + 掷骰子决定联不联系」—— 改为【消息驱动】
      （用户每说一句即一次回合；消息数达 auto_day_turns 时自然跨天，也可手动 --day）

完全复用 L0 内核（不改动任何既有文件）：
  - phase5_common.build_world         从任意 start_day 冻结世界（persona + 当日关系状态）
  - phase5_a2_loop.PersonaAgent(B)    B 的「决策-行动」（人格 + S 层情绪 + 媒介抽样 + 去重护栏）
  - phase5_a2_loop.retrieve_memories  记忆检索视图（含 as_of 时间过滤 + 分歧点冻结真实未来）
  - phase5_a3_buffer.WorkingMemory    短期对话缓冲
  - phase6_engine.update_rel_state_engine  关系状态（事件驱动 + 稳态回归）
  - phase5_a2_loop.log_event          事件落库（sim_events + sim_facts，隔离命名空间）

隔离红线：全部写入 sim_* 命名空间；主库 messages / facts / events / relationship_state 零侵入。
隐私：LLM 只接收 content_clean 派生的 persona/记忆/对话文本；本模块不读原始 JSONL。

用法（项目根执行；需要 LLM API Key 的步骤见各条）:
  # ① 新建一条线：从聊天记录结束之后继续聊（默认起点=最后一条消息的次日）
  python app/phase15_dial_engine.py new --name "继续聊"

  # ② 从历史某天切入 + 自由改写（IF 线）
  python app/phase15_dial_engine.py new --start 2024-06-20 --divergence 2024-06-26 \
      --rewrite "那天我没有只回一个『嗯』，我直接打了电话过去" --name "如果那天我打了电话"

  # ③ 说一句话 / 交互式连续聊
  python app/phase15_dial_engine.py say "在忙吗" --line last
  python app/phase15_dial_engine.py repl --line last

  # ④ 查看
  python app/phase15_dial_engine.py list
  python app/phase15_dial_engine.py show --line last --tail 30

  # ⑤ 离线自检（不需要 API key，用桩客户端验证全链路）
  python app/phase15_dial_engine.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import config as cfg_mod
import phase17_observe as observe
import phase17_state as bstate
import phase17_stance as st17
import phase17_action as act17
import phase4_retrieval
import phase5_a2_loop as loop


# ---------------------------------------------------------------- 引擎模式（§12 统一开关）
# IFWE_ENGINE_MODE = legacy | phase16_fixes | phase17_stance_action
# 未设置时按旧开关映射（向后兼容）：IFWE_PERSONA_FIDELITY=0 → legacy，否则 phase16_fixes。
def _engine_mode() -> str:
    m = (os.environ.get("IFWE_ENGINE_MODE", "") or "").strip().lower()
    if m in ("legacy", "phase16_fixes", "phase17_stance_action"):
        return m
    if (os.environ.get("IFWE_PERSONA_FIDELITY", "") or "").strip().lower() in             {"0", "false", "off", "no"}:
        return "legacy"
    return "phase16_fixes"
import phase5_a3_buffer as buf
import phase5_common as pc
import phase6_engine as p6
from phase5_llm import AgentReply, get_client

HUMAN = "A"          # 用户本人
PARTNER = "B"        # 数字人格
KIND = "dial"        # 线类型标记（写入 sim_runs.config_json）

# 口径=【回合计数】（§5.9：一次 say() 计一回合，沉默计入；数据出处：真实有消息日均 19.4 条）
AUTO_DAY_TURNS = int(cfg_mod.load()["defaults"]["auto_day_turns"])
MEM_TOP_K = 5
# 同类型事件当天最多计权次数（第 2 次衰减 ≈0.5 倍，第 3 次起不计）
# 依据：关系状态是「月粒度」估计，一天内重复的同类型互动不应线性刷高五维
MAX_SAME_EVENT_PER_DAY = 2


def get_default_start(conn: sqlite3.Connection) -> str:
    """「接着往下聊」的默认起点：start_after_last=true 时取最后一条消息的次日，
    否则取最后一条消息当天；无数据时退回今天"""
    try:
        row = conn.execute("SELECT MAX(day) FROM messages WHERE day IS NOT NULL").fetchone()
        if row and row[0]:
            last = date.fromisoformat(row[0])
            if cfg_mod.load()["defaults"]["start_after_last"]:
                last = last + timedelta(days=1)
            return last.isoformat()
    except sqlite3.Error:
        pass
    return date.today().isoformat()

DIM_LABEL = p6.DIM_LABEL

SCHEMA_IF = cfg_mod.resource_dir() / "schema_if.sql"


def _apply_branch_schema(conn: sqlite3.Connection) -> None:
    """确保 ifr_branch（分支元数据表）存在（幂等）"""
    conn.executescript(SCHEMA_IF.read_text(encoding="utf-8"))
    conn.commit()


# ---------------------------------------------------------------- 产品态附加人格约束
DIAL_RULES = """
【聊天场景（产品态，优先于上面的一般规则）】
你正在用微信和对方聊天，只输出你**实际会发出的那条消息**，不要旁白、不要分析、不要总结。
- 长度贴近你的真实习惯：绝大多数消息很短（几个字到十几个字），偶尔才有一条长一点的。
- 允许只回一两个字、允许答非所问、允许用语气词收尾（的/吗/吧/呢/嗯/啊），允许突然岔开话题。
- 不要每条都关心对方、不要每条都推进关系、不要复述或概括对方刚说的话、不要分点、不要排比。
- **不要连续几轮表达同一个意思**（例如反复催对方去睡觉、反复说"明天再说"）。若上一轮已说过，
  这一轮就换一种真实反应——哪怕只回一个字、一个语气词，或干脆发个表情包、不接话。
- 不要提及你不可能知道的未来信息，也不要引用档案之外的经历。
- action 只在确有并列动作时填写（例如「隔了很久才回」）；没有就留空串。
"""


# ---------------------------------------------------------------- 人格保真（F1–F4）
# ⚠ 本块必须在 DIAL_RULES 定义之后执行（否则 NameError 会被 except 吞掉、改动全部失效）。
# pf 不可用时回退原行为：pf=None，say() 跳过意愿判定与原文窗口，规则用原常量。
try:
    import persona_fidelity as pf
    pf.apply(loop)                             # F3: REPLY_RULES 替换 / F4: 去重护栏豁免
    DIAL_RULES = pf.dial_rules(DIAL_RULES)     # F3: 产品态规则按情境取长度（关闭时原样返回）
except Exception as _pf_err:                   # 失败必须可见，不许静默
    print(f"[warn] persona_fidelity 未生效，人格保真改动回退原行为: {_pf_err}")
    pf = None


# ---------------------------------------------------------------- 事件启发式判定（零 token）
# 顺序即优先级：命中多个时取优先级最高的一个
EVENT_PRIORITY = [
    "冲突/分歧", "冲突修复/和好", "情绪低谷/安慰", "见面/出行", "纪念日/承诺",
    "娱乐互动", "学业考试/工作求职", "家人/朋友", "健康", "金钱/转账",
    "高兴/庆祝", "日常陪伴",
]

EVENT_KEYWORDS: dict[str, list[str]] = {
    "冲突/分歧": ["生气", "不理", "算了", "别说了", "别烦", "吵", "失望", "分手", "拉黑",
                  "不想理", "你怎么这样", "过分", "受够", "凭什么", "闹别扭", "冷淡", "敷衍",
                  "不回我", "不想说", "别找我", "没意思", "别这样"],
    "冲突修复/和好": ["对不起", "抱歉", "我错了", "原谅", "和好", "别气了", "哄", "不怪你", "我不好",
                      "别生气", "是我不好", "不会再"],
    "情绪低谷/安慰": ["难受", "崩溃", "累", "压力", "不开心", "难过", "焦虑", "哭", "想哭",
                      "安慰", "抱抱", "别难过", "辛苦", "撑不住", "emo", "低落", "烦躁",
                      "好烦", "很烦", "真烦", "太烦", "烦死",
                      "委屈", "没劲", "心累", "好丧", "很丧", "太丧", "睡不好",
                      "撑不下去", "没力气"],
    "见面/出行": ["见面", "见一面", "没见", "好久不见", "想见", "去哪", "一起吃",
                  "来找我", "去找你", "车票", "地铁口", "我到了", "我到站", "出发",
                  "过去看", "见个面", "什么时候见", "来看你", "去找你玩"],
    "纪念日/承诺": ["纪念日", "周年", "约定", "答应你", "承诺", "一直在一起", "一直陪着", "一辈子"],
    "娱乐互动": ["游戏", "打一局", "上号", "看剧", "追剧", "动漫", "视频", "表情包", "梗图",
                 "笑死", "哈哈", "打游戏"],
    "学业考试/工作求职": ["论文", "答辩", "面试", "工作", "投简历", "考试", "导师", "项目", "加班", "offer"],
    "家人/朋友": ["我妈", "我爸", "朋友", "同学", "家里", "姐姐", "爸妈"],
    "健康": ["生病", "感冒", "头疼", "胃疼", "胃痛", "医院", "发烧", "睡不着", "失眠", "不舒服"],
    "金钱/转账": ["转账", "红包", "借钱", "还钱", "买单", "付了", "多少钱"],
    "高兴/庆祝": ["太好了", "开心", "庆祝", "成功", "考过了", "面过了", "上岸", "顺利", "恭喜"],
}

# 否定前缀防护：≤2 字的短词若紧跟在否定字之后，视为误命中（如「不累」「没见面」）
NEG_PREFIX = "不没别勿非莫"


def _keyword_hit(blob: str, kw: str) -> bool:
    """子串匹配 + 短词否定防护（避免『构建出来』命中『出来』这类误判）"""
    if len(kw) >= 3:
        return kw in blob
    start = 0
    while True:
        i = blob.find(kw, start)
        if i < 0:
            return False
        if i == 0 or blob[i - 1] not in NEG_PREFIX:
            return True
        start = i + 1

# (severity, importance) 基准；severity=负面/情绪波动强度，importance=对关系的长期影响
EVENT_SEV_IMP = {
    "冲突/分歧": (4, 4),
    "冲突修复/和好": (3, 4),
    "情绪低谷/安慰": (3, 3),
    "见面/出行": (2, 3),
    "纪念日/承诺": (2, 4),
    "娱乐互动": (1, 2),
    "学业考试/工作求职": (2, 2),
    "家人/朋友": (2, 2),
    "健康": (2, 2),
    "金钱/转账": (1, 2),
    "高兴/庆祝": (2, 2),
    "日常陪伴": (1, 1),
}


# 真实表情包文件名（WeFlow 导出落在项目外；与 phase15_api.MEDIA_DIR 对应）
STICKER_NAME = re.compile(r"^[0-9a-f]{32}\.(gif|jpg|jpeg|png)$", re.I)


def classify_user_event(user_text: str) -> dict:
    """零成本启发式：只看【用户消息】判定 0-1 个关系事件（二期 §5.7，与私有树同名同源）。

    与旧 classify_event(user_text, reply_text) 的区别（§5.7 硬要求）：
      - 删掉 reply_text 入参：生成文本不再驱动关系状态；
      - 在回复决策【之前】调用；
      - 兜底「日常陪伴」只在 B 确实回应（有来回）时由 say() 补上；
        reply_mode=none（沉默）→ 记「未回复」，不计权。
    事件类型仍取 12 类坐标系（§14：保留 + 扩展，勿删）。
    """
    blob = user_text or ""
    for et in EVENT_PRIORITY:
        kws = EVENT_KEYWORDS.get(et)
        if kws is None:            # 日常陪伴：兜底（有来回才启用，见 say()）
            continue
        if any(_keyword_hit(blob, k) for k in kws):
            sev, imp = EVENT_SEV_IMP[et]
            # 情绪强度微调：出现连续感叹/问号则升一级
            if ("！！" in blob or "??" in blob or "？？" in blob) and sev < 5:
                sev += 1
            return {"event_type": et, "severity": sev, "importance": imp}
    return {}


# ---------------------------------------------------------------- 桩客户端（离线自检用）
class StubClient:
    """不联网的替身：用于 selftest 验证「世界构建→检索→回合→落库→关系更新」全链路。"""

    name = "stub"

    def extract(self, system: str = "", user: str = "", response_model=None, **kw):
        if response_model is AgentReply:
            return AgentReply(reply="嗯，看到了", medium="text",
                              emotion={"label": "平静", "level": 5.0}, action="")
        try:
            return response_model()
        except Exception:
            return None


# ---------------------------------------------------------------- 对话内核
class DialEngine:
    """一条「线」= 一个 sim_id。承载：世界状态 + B 的数字人格 + 对话缓冲 + 分支内关系状态。"""

    def __init__(self, conn: sqlite3.Connection, sim_id: str,
                 client=None, retriever=None, verbose: bool = True):
        self.conn = conn
        self.sim_id = sim_id
        self.client = client
        self.retriever = retriever
        self.verbose = verbose

        row = conn.execute(
            "SELECT branch_name, start_day, divergence_point, divergence_desc, rewritten_choice, "
            "config_json FROM sim_runs WHERE sim_id=?", (sim_id,)).fetchone()
        if not row:
            raise ValueError(f"线不存在: {sim_id}")
        self.branch_name, self.start_day, self.divergence_point, self.divergence_desc, \
            self.rewritten_choice, cfg_raw = row
        self.cfg = json.loads(cfg_raw or "{}")
        # Phase A 路径标记：kind=replay 的线（回放装置）→ path=replay，其余 product；
        # 只影响观测落库，不影响任何行为。
        self.path = observe.PATH_REPLAY if self.cfg.get("kind") == "replay" \
            else observe.PATH_PRODUCT
        self.run_state = self.cfg.setdefault("run_state", {
            "day_start_rel": None, "day_events": [], "rel_current": None,
            "session_id": None, "session_day": None, "last_day": None,
        })
        # §5.9：sim_messages 对话时间字段（加法迁移，幂等）——任何写入都带 sent_at/time_source
        pc.ensure_time_columns(conn)

        # ---- 世界（复用 build_world：persona + 截至 start_day 的真实关系状态）----
        self.world = pc.build_world(
            conn, self.run_state.get("last_day") or self.start_day,
            seed=self.cfg.get("seed") or 0,
            branch_name=self.branch_name, branch_of=self.cfg.get("branch_of", "main"),
            divergence_point=self.divergence_point,
            divergence_desc=self.divergence_desc or "",
            rewritten_choice=self.rewritten_choice,
            sim_id=self.sim_id)
        self.world.clock.set_end("2099-12-31")

        # ---- B 的数字人格（只实例化 B；A 由用户本人替代）----
        # F4 去重护栏豁免 = 可注入策略（§10.9）：短回复（≤对方真实长度中位数，数据推导）
        # 豁免改写，长复读仍保留护栏；boundary 行动的豁免在 PersonaAgent._should_rewrite 内。
        _f4_med = None
        if pf is not None:
            try:
                _f4_med = pf.partner_median_len(self.conn)
                pf.register_exempt_len(_f4_med)      # 兼容旧路径语义
            except Exception:
                _f4_med = None

        def _f4_similarity_policy(text: str):
            """F4 策略：短回复豁免（False）、长文本交回默认 bigram 判定（None）。"""
            if _f4_med is not None and len((text or "").strip()) <= _f4_med:
                return False
            return None

        self.partner = loop.PersonaAgent(PARTNER, self.world,
                                         similarity_policy=_f4_similarity_policy)
        self.partner.persona_block = self.partner.persona_block + "\n" + DIAL_RULES
        self.partner.block_lite = self.partner.block_lite + "\n" + DIAL_RULES

        # ---- 引擎模式与当前立场（Phase C · §6.1C）：分支起点推断一次并缓存 ----
        self.phase17 = _engine_mode() == "phase17_stance_action"   # 运行期读取（测试可切换）
        self._stance = None
        if self.phase17 and pf is not None:
            try:
                est = pf.estimate_reply_probability(
                    self.conn, sim_id=self.sim_id, start_day=self.start_day,
                    current_day=self.start_day)
                self._stance = st17.infer_stance(self.conn, self.start_day,
                                                 p_anchor=est.get("p"))
            except Exception as e:
                if self.verbose:
                    print(f"  [warn] 立场推断失败（不阻断，MVP 用空立场）: {str(e)[:80]}")
                self._stance = st17.infer_stance(self.conn, self.start_day, p_anchor=None)
            self._meta_detector = st17.make_meta_detector(self.client)   # §8.2 结构化判定
            # §11 Phase E：表达层画像（真实 B 消息当前段推导）→ 媒介抽样条件化
            try:
                import phase17_expression as ex
                self._profile = ex.expression_profile(self.conn, self.start_day)
                if self._profile.get("medium_probs"):
                    self.partner.medium_probs = {
                        "A": dict(self.partner.medium_probs.get("A")
                                  or self._profile["medium_probs"]),
                        "B": dict(self._profile["medium_probs"])}
            except Exception as e:
                if self.verbose:
                    print(f"  [warn] 表达画像失败（用默认媒介倾向）: {str(e)[:80]}")
                self._profile = None

        # ---- 短期缓冲：从库中重建（保证进程重启后上下文不断）----
        self.wm = buf.WorkingMemory()
        for day, sender, content in reversed(self._recent_rows(24)):
            self.wm.add(day, sender, content)

        # ---- 分支内关系状态 ----
        # §5.10：优先从 sim_branch_state 恢复（含 PersonaAgent 的 s_state/calib/
        # recent_texts）；sim_runs.run_state 仅作向后兼容回退。
        restored = bstate.load(conn, self.sim_id) or {}
        if restored.get("rel_current"):
            self.rel_current = dict(restored["rel_current"])
        elif self.run_state.get("rel_current"):
            self.rel_current = dict(self.run_state["rel_current"])
        else:
            self.rel_current = dict(self.world.rel_state or {}) or None
            if self.rel_current:
                self.run_state["rel_current"] = dict(self.rel_current)
        if restored.get("s_state"):
            self.partner.s_state = dict(restored["s_state"])
        if restored.get("calib") is not None:
            self.partner.calib = restored["calib"]
        if restored.get("recent_texts"):
            self.partner.recent_texts = list(restored["recent_texts"])
        if not self.run_state.get("day_start_rel") and self.rel_current:
            self.run_state["day_start_rel"] = dict(self.rel_current)

    # ---------------- 库读取 ----------------
    def _recent_rows(self, n: int) -> list[tuple]:
        rows = self.conn.execute(
            "SELECT day, sender, content FROM sim_messages WHERE sim_id=? "
            "ORDER BY rowid DESC LIMIT ?", (self.sim_id, n)).fetchall()
        return rows

    def _ensure_session(self, day: str) -> str:
        """每天一个会话；跨天自动新建（复用 sim_sessions）"""
        sid = self.run_state.get("session_id")
        if sid and self.run_state.get("session_day") == day:
            return sid
        seq = (self.conn.execute("SELECT MAX(seq) FROM sim_sessions WHERE sim_id=?",
                                 (self.sim_id,)).fetchone()[0] or 0) + 1
        sid = f"{self.sim_id}-S{seq:03d}"
        self.conn.execute(
            "INSERT OR REPLACE INTO sim_sessions (session_id, sim_id, seq, day, n_turns, "
            "initiator, created_at) VALUES (?,?,?,?,0,?,?)",
            (sid, self.sim_id, seq, day, HUMAN, pc.now_str()))
        self.conn.commit()
        self.run_state["session_id"] = sid
        self.run_state["session_day"] = day
        self._save_state()
        return sid

    def _next_turn(self, session_id: str) -> int:
        """轮次取 MAX+1（非 COUNT）：消息被部分删除后 COUNT 会回退，导致 msg_id 主键冲突"""
        row = self.conn.execute("SELECT MAX(turn_idx) FROM sim_messages WHERE session_id=?",
                                (session_id,)).fetchone()
        return (row[0] + 1) if row and row[0] is not None else 0

    def _put_message(self, session_id: str, day: str, sender: str, content: str,
                     emotion_s: str = "", meta: dict | None = None) -> int:
        """落一条消息。§5.9：sent_at=对话时间（A=真实输入时刻 user_input；
        B=分支日 simulated，模拟侧无对话时刻，不伪造精度、不读 created_at）。
        §5.2：B 侧 meta.kind 显式区分 reply / silent，便于审计与统计排除。"""
        turn = self._next_turn(session_id)
        if sender == HUMAN:
            sent_at, tsrc = pc.now_str(), "user_input"
        else:
            sent_at, tsrc = day, "simulated"
        if sender == PARTNER:
            kind = "silent" if not (content or "").strip() else "reply"
            meta = {"kind": kind, **(meta or {})}
        self.conn.execute(
            "INSERT OR REPLACE INTO sim_messages (msg_id, sim_id, session_id, day, turn_idx, "
            "sender, content, emotion_s, meta, created_at, sent_at, time_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{session_id}-T{turn:03d}", self.sim_id, session_id, day, turn,
             sender, content, emotion_s, json.dumps(meta or {}, ensure_ascii=False),
             pc.now_str(), sent_at, tsrc))
        self.conn.execute("UPDATE sim_sessions SET n_turns=? WHERE session_id=?",
                          (turn + 1, session_id))
        self.conn.commit()
        return turn

    def _rounds_in_session(self, session_id: str) -> int:
        """本会话已进行的回合计数（§5.9：口径=用户消息条数；沉默计入回合）。"""
        return self.conn.execute(
            "SELECT COUNT(*) FROM sim_messages WHERE session_id=? AND sender=?",
            (session_id, HUMAN)).fetchone()[0]

    def _save_state(self) -> None:
        self.cfg["run_state"] = self.run_state
        self.cfg["kind"] = KIND
        self.conn.execute("UPDATE sim_runs SET config_json=? WHERE sim_id=?",
                          (json.dumps(self.cfg, ensure_ascii=False), self.sim_id))
        # §5.10：同时写 sim_branch_state（结构化、带版本；run_state 为向后兼容）
        try:
            decision_tail = None
            try:
                ds = observe.read_decisions(self.conn, self.sim_id, limit=1)
                decision_tail = ds[-1] if ds else None
            except Exception:
                pass
            bstate.save(self.conn, self.sim_id,
                        rel_current=self.rel_current,
                        s_state=getattr(self.partner, "s_state", None),
                        calib=getattr(self.partner, "calib", None),
                        recent_texts=getattr(self.partner, "recent_texts", None),
                        decision_tail=decision_tail)
        except Exception as e:
            if self.verbose:
                print(f"  [warn] 分支状态落库失败（不影响对话）: {str(e)[:80]}", flush=True)
        self.conn.commit()

    def _silent_return(self, session_id: str, day: str, turn_a: int, user_text: str,
                       action, snap, rel_before: dict, gate_reason, auto_day: bool) -> dict:
        """phase17 模式沉默轮收尾：落 silent 标记、决策观测、回合计数、状态保存"""
        p_rep = action.p_reply or 0.0
        self._put_message(session_id, day, PARTNER, "",
                          meta={"kind": "silent", "medium": "none", "action": "",
                                "human": False, "silent": True,
                                "p_reply": round(p_rep, 3)})
        self._log_decision(session_id=session_id, day=day, turn_idx=turn_a,
                           decided=0, snapshot=snap, p_reply=p_rep,
                           reply_mode="none",
                           silent_reason=gate_reason,
                           state_before=rel_before,
                           state_after=dict(self.rel_current or {}),
                           source=action.model_version)
        if self.verbose:
            print(f"  ·  她没回（P(reply)={p_rep:.2f}，{action.decision_reason[:40]}）")
        advanced = None
        if auto_day and self._rounds_in_session(session_id) >= AUTO_DAY_TURNS:
            advanced = self.advance_day(1)      # §5.9：沉默计入回合
        self._save_state()                       # §5.10
        return {"day": day, "user": user_text, "reply": "", "medium": "none",
                "action": "", "sticker": None, "emotion": None, "event": None,
                "event_note": {"event_type": "未回复", "times": 1, "counted": False,
                               "note": f"她没回（P(reply)={p_rep:.2f}）"},
                "rel": dict(self.rel_current or {}), "session_id": session_id,
                "advanced_to": advanced, "silent": True, "willingness": p_rep}

    # ---------------- 关系状态（当天累计事件 + 幂等重算）----------------
    def _commit_rel(self, day: str) -> None:
        base = self.run_state.get("day_start_rel") or self.rel_current
        if not base:
            return
        events = self.run_state.get("day_events") or []
        if self.phase17:
            # 【§5.8 · Phase C】关系增量门控：产品路径下事件增量经立场门控
            # （拒绝立场/极低联系档 → 正向维不加热），仍为幂等重算（base+全部门控增量）。
            # 12 类坐标系保留（事件照记照算），只改「增量是否生效」。
            eng = p6.RelEngine()
            anchor = self.world.rel_state or {"closeness": 5.26, "conflict": 4.25,
                                              "trust": 4.54, "emotional_safety": 3.31,
                                              "comm_quality": 8.52}
            s = dict(base)
            for ev in events:
                gd = st17.gated_delta(self._stance or {}, eng.event_delta(ev))
                for d, v in gd.items():
                    s[d] = s.get(d, 5.0) + v
            for d in p6.DIMS:
                b = anchor.get(d) or 5.0
                s[d] = round(min(10.0, max(0.0, 0.99 * s.get(d, b) + 0.01 * b)), 3)
            self.conn.execute(
                """INSERT OR REPLACE INTO sim_rel_state
                   (sim_id, day, closeness, conflict, trust, emotional_safety, comm_quality,
                    confidence, method, notes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (self.sim_id, day, s["closeness"], s["conflict"], s["trust"],
                 s["emotional_safety"], s["comm_quality"], 0.45, "engine_gated",
                 "phase17 立场门控增量 + 0.99 稳态回归锚点"))
            self.conn.commit()
            self.rel_current = s
        else:
            self.rel_current = p6.update_rel_state_engine(
                self.conn, self.sim_id, day, dict(base), events, anchor=self.world.rel_state)
        self.run_state["rel_current"] = dict(self.rel_current)
        self._save_state()

    def _anchored_p(self) -> float:
        """分支锚定 P（裁定三）：优先取立场推断时缓存的 p_anchor；
        无值（推断失败/无样本）→ config fallback（§5.11 同语义，不得 0.0）。"""
        try:
            pa = (self._stance or {}).get("p_anchor")
            if pa is not None:
                return float(pa)
        except Exception:
            pass
        try:
            return float(pf._wcfg().get("fallback", 0.5))
        except Exception:
            return 0.5

    def _note_event(self, day: str, session_id: str, ev: dict, why: str) -> Optional[dict]:
        """当天事件计权：同类型第 1 次全额；第 2 次衰减（sev/imp 降级 ≈0.5 倍）；
        第 3 次起不再累加。三种情况都回传说明，避免「数据不动」看起来像坏了。"""
        if not ev:
            return None
        et = ev["event_type"]
        events = self.run_state.setdefault("day_events", [])
        n = sum(1 for e in events if e.get("event_type") == et) + 1
        if n > MAX_SAME_EVENT_PER_DAY:
            return {"event_type": et, "times": n, "counted": False,
                    "note": f"{et}（本日第{n}次·已达上限，不重复累加）"}
        if n == 2:
            ev = {**ev, "severity": 1, "importance": 1}       # 衰减计权（scale≈0.54）
            note = f"{et}（本日第2次·衰减计权）"
        else:
            note = et
        ev["times"] = n
        events.append(ev)
        loop.log_event(self.conn, self.sim_id, day,
                       {**ev, "summary": (ev.get("summary") or why)[:30]}, session_id)
        self._commit_rel(day)
        return {"event_type": et, "times": n, "counted": True, "note": note}

    def _log_decision(self, **kw) -> None:
        """Phase A 决策观测：失败只警告，绝不连累对话主流程（观测不改变行为）"""
        try:
            observe.log_decision(self.conn, sim_id=self.sim_id,
                                 path=self.path, **kw)
        except Exception as e:
            if self.verbose:
                print(f"  [warn] 决策观测落库失败: {str(e)[:80]}", flush=True)

    # ---------------- 真实表情包 ----------------
    def _available_media(self) -> set[str]:
        """媒体库里的文件名集合（v0.3 媒体导入通道；缺表/缺列时不报错，返回空集）。"""
        try:
            rows = self.conn.execute("SELECT filename FROM media").fetchall()
        except Exception:
            return set()
        return {(r[0] or "") for r in rows if r[0]}

    def pick_partner_sticker(self) -> Optional[str]:
        """从【对方真实用过的】表情包里按使用频率加权抽一张（真图，非生成）。

        v0.3 起候选判定放宽：命中媒体库文件名即可（自己导入的表情包文件名不限格式），
        同时保留旧的 32 位 hex 命名以便兼容未走媒体导入的老用户。
        """
        rows = self.conn.execute(
            "SELECT attachment_name, COUNT(*) AS c FROM messages "
            "WHERE subtype='emoji_gif' AND sender_key=? AND attachment_name IS NOT NULL "
            "GROUP BY attachment_name", (PARTNER,)).fetchall()
        known = self._available_media()
        cand = [(a, c) for a, c in rows
                if (a in known) or STICKER_NAME.match(a or "")]
        if not cand:
            return None
        total = sum(c for _, c in cand)
        roll = self.world.clock.rng.random() * total
        acc = 0.0
        for name, c in cand:
            acc += c
            if roll < acc:
                return name
        return cand[-1][0]

    # ---------------- 跨天 ----------------
    def advance_day(self, n: int = 1) -> str:
        old = self.world.day
        day = self.world.clock.forward(n)
        self.run_state["last_day"] = day
        self.run_state["day_start_rel"] = dict(self.rel_current or {})
        self.run_state["day_events"] = []
        self.run_state["session_id"] = None
        self.run_state["session_day"] = None
        self._save_state()
        if self.verbose:
            print(f"  ── 时间来到 {day}（自 {old}）──")
        return day

    # ---------------- 主动开口（§6.1E initiate · §11 Phase E）----------------
    def initiate_turn(self) -> dict | None:
        """她主动开口的一轮（无用户输入）。掷骰概率=真实 B 先开口天占比；
        拒绝立场下开场约束为事务性简短（立场约束优先于表达层）。"""
        if not self.phase17 or not getattr(self, "_profile", None):
            return None
        import phase17_expression as ex
        if not ex.initiate_check(self._profile, self.world.clock.rng):
            return None
        day = self.world.day
        session_id = self._ensure_session(day)
        turn_idx = self._next_turn(session_id)
        rejected = (self._stance or {}).get("romantic_intent") == "明确拒绝"
        action = act17.ActionDecision(
            reply_mode="initiate", should_generate=True,
            p_reply=self._anchored_p(), scene_mode="主动开口",
            event_type="日常陪伴" if not rejected else None,
            must_not_do=["推进关系", "表达思念", "重提复合"] if rejected else [],
            generation_hint=("【本轮行动约束】由你主动开口。"
                             + ("保持事务性、简短（如分享一件具体小事），"
                                "不做情感推进、不表达思念。" if rejected
                                else "自然开场，符合当前关系状态。")),
            decision_reason=f"主动开口掷骰命中（真实开口率={self._profile.get('initiation_rate')}）",
            evidence_ids=list((self._stance or {}).get("evidence_ids") or []),
            initiate=True)
        act17.apply_expression(action, self._profile)
        act17.log_action(self.conn, sim_id=self.sim_id, session_id=session_id,
                         day=day, turn_idx=turn_idx, path=self.path, action=action)
        ws_text = self.world.summary_text(with_memories=False,
                                          rel_override=self.rel_current)
        reply_obj = self.partner.act(self.client, ws_text, "", self.wm.render(),
                                     "（她主动发来一条消息）", rng=self.world.clock.rng,
                                     full_persona=True, action=action)
        self._put_message(session_id, day, PARTNER, reply_obj.reply or "",
                          emotion_s=str((reply_obj.emotion or {}).get("label", "")),
                          meta={"medium": reply_obj.medium, "action": reply_obj.action,
                                "human": False, "initiated": True, "kind": "reply"})
        self.wm.add(day, PARTNER, reply_obj.reply or "")
        if not rejected and action.event_type:
            try:
                self._note_event(day, session_id,
                                 {"event_type": action.event_type, "severity": 1,
                                  "importance": 1,
                                  "summary": f"主动开口：{(reply_obj.reply or '')[:14]}"},
                                 "initiate")
            except Exception:
                pass
        self._save_state()
        return {"day": day, "reply": reply_obj.reply or "", "medium": reply_obj.medium,
                "action_obj": {"reply_mode": action.reply_mode,
                               "decision_reason": action.decision_reason},
                "session_id": session_id}

    # ---------------- 核心：一次往返 ----------------
    def say(self, user_text: str, auto_day: bool = True) -> dict:
        user_text = (user_text or "").strip()
        if not user_text:
            raise ValueError("消息为空")
        day = self.world.day
        session_id = self._ensure_session(day)

        # 1) 用户消息（human 标记，永不改写成 A 的 LLM 版本）
        turn_a = self._put_message(session_id, day, HUMAN, user_text,
                                   meta={"human": True})
        self.wm.add(day, HUMAN, user_text)
        rel_before = dict(self.rel_current or {})      # Phase A：决策前状态快照

        # 1.2)【§5.7】先识别本轮用户事件（零 token，只看用户消息），再决定 B 的行动
        ev_user = classify_user_event(user_text)

        # 1.3)【§6.1D/E · Phase C】本轮场景与行动决策（生成前结构化决定，落库 sim_action_log）
        action = None
        if self.phase17:
            try:
                scene17 = st17.infer_scene(user_text, ev_user, self._stance or {},
                                       meta_detector=self._meta_detector)
                action = act17.decide(self._stance or {}, scene17, ev_user,
                                      p_reply=self._anchored_p(), rng=self.world.clock.rng,
                                      profile=self._profile)
                act17.apply_expression(action, self._profile)
                act17.log_action(self.conn, sim_id=self.sim_id, session_id=session_id,
                                 day=day, turn_idx=turn_a, path=self.path, action=action)
            except Exception as e:
                if self.verbose:
                    print(f"  [warn] 行动决策失败（回退到既有闸门）: {str(e)[:80]}", flush=True)
                action = None

        # 1.5) F1 回复意愿（架构级）：判不回则不生成、不调 LLM、落库沉默标记并返回。
        #      沉默行仍计入会话轮次（时间照常流逝）；事件判定整段跳过——
        #      否则空 reply 会兜底成「日常陪伴」，对方明明沉默、关系却还在被加热。
        willingness = None
        gate_reason = None          # 闸门回退原因（Phase A：所有回退必须有原因）
        snap = None                 # 决策时刻意愿估计快照（Phase A 观测）
        if action is not None:
            # phase17 模式：行动决策即闸门（should_generate 已按锚定 P 掷骰）
            willingness = action.p_reply
            gate_reason = (None if action.should_generate
                           else f"not_replied;action=none;{action.decision_reason[:60]}")
            snap = observe.snapshot_estimate(self.conn, self.sim_id, day)
            if not action.should_generate:
                return self._silent_return(session_id, day, turn_a, user_text, action,
                                           snap, rel_before, gate_reason, auto_day)
        elif pf is not None and pf.f1_enabled():
            try:
                est = pf.estimate_reply_probability(
                    self.conn, sim_id=self.sim_id, start_day=self.start_day,
                    current_day=day, divergence_point=self.divergence_point)
            except Exception as e:
                # 【§5.11】异常时按 config fallback 处理并记录失败原因，
                # 不得默认 p=1.0（那等于「出问题就更热情」，方向错误）。
                try:
                    fb = float(pf._wcfg().get("fallback", 0.5))
                except Exception:
                    fb = 0.5
                est = {"p": fb, "layer": "fallback", "samples": 0}
                gate_reason = f"decision_error;p_reply_source=error;{str(e)[:60]}"
            willingness = est["p"]
            snap = observe.snapshot_estimate(self.conn, self.sim_id, day)
            if self.world.clock.rng.random() > willingness:
                if self.verbose:
                    print(f"  [意愿] TA 没回（P={willingness:.2f}，{est['layer']}，"
                          f"近窗样本 {est['samples']}）")
                self._put_message(session_id, day, PARTNER, "",
                                  meta={"silent": True, "human": False,
                                        "willingness": round(willingness, 4)})
                advanced = None
                if auto_day and self._rounds_in_session(session_id) >= AUTO_DAY_TURNS:
                    advanced = self.advance_day(1)      # §5.9：沉默计入回合
                self._save_state()                       # §5.10：沉默轮也落分支状态
                # Phase A：沉默轮决策观测
                self._log_decision(session_id=session_id, day=day, turn_idx=turn_a,
                                   decided=0, snapshot=snap, p_reply=willingness,
                                   reply_mode="none",
                                   silent_reason=(f"not_replied;layer={est['layer']} "
                                                  f"n={est['samples']}"),
                                   state_before=rel_before,
                                   state_after=dict(self.rel_current or {}),
                                   source="ported")
                return {"day": day, "user": user_text, "reply": "", "medium": "none",
                        "action": "", "sticker": None, "emotion": None,
                        "event": None,
                        "event_note": {"event_type": "未回复", "times": 1,
                                       "counted": False,
                                       "note": f"她没回（P(reply)={willingness:.2f}）"},
                        "rel": dict(self.rel_current or {}),
                        "session_id": session_id, "advanced_to": advanced,
                        "silent": True, "willingness": willingness}
        elif pf is not None:
            gate_reason = "gate_skipped;f1_disabled"
        else:
            gate_reason = "gate_skipped;pf_unavailable"

        # 2) 上下文装配：真实原文窗口（F2，置于最前）+ 世界 + 改写场景卡 + 记忆 + 缓冲
        # 【§5.6】生成上下文里的状态视图改为分支当前状态 rel_current（「本分支，推断」）
        ws_text = self.world.summary_text(with_memories=False,
                                          rel_override=self.rel_current)
        if pf is not None and pf.f2_enabled():
            try:
                win = pf.real_window_text(self.conn, start_day=self.start_day,
                                          divergence_point=self.divergence_point,
                                          has_rewrite=bool(self.rewritten_choice))
            except Exception as e:
                win = ""
                if self.verbose:
                    print(f"  [warn] 真实窗口加载失败: {str(e)[:80]}")
            if win:
                ws_text = win + "\n\n" + ws_text
        scene = self.world.divergence_scene()      # 分歧日只注入一次
        if scene:
            ws_text = ws_text + "\n" + scene
        query = user_text[:80]
        mem_text = ""
        if self.retriever is not None:
            try:
                mem_text = loop.retrieve_memories(self.retriever, self.conn, self.world,
                                                  query, day, top_k=MEM_TOP_K)
            except Exception as e:
                if self.verbose:
                    print(f"  [warn] 记忆检索失败: {str(e)[:80]}")

        # 3) B 的回应（复用 PersonaAgent.act；full_persona=产品态人格保真优先）
        buffer_text = self.wm.render()
        # Phase A：装配摘要（只存 sha 前 16 位 + 组件长度，不落原文）
        pdigest = observe.prompt_digest({
            "persona": self.partner.persona_block, "ws": ws_text,
            "mem": mem_text, "buf": buffer_text, "user": user_text})
        # phase17：行动约束注入生成提示；boundary 轮豁免去重护栏（§6.2 步骤 7/T10）
        reply_obj = self.partner.act(self.client, ws_text, mem_text, buffer_text,
                                     user_text, rng=self.world.clock.rng, full_persona=True,
                                     action=action)

        # 4) 落库 + 缓冲（媒介为表情包时 → 换上对方真实用过的表情图）
        sticker = self.pick_partner_sticker() if reply_obj.medium == "emoji" else None
        self._put_message(session_id, day, PARTNER, reply_obj.reply or "",
                          emotion_s=str((reply_obj.emotion or {}).get("label", "")),
                          meta={"medium": reply_obj.medium, "action": reply_obj.action,
                                "human": False, "sticker": sticker})
        self.wm.add(day, PARTNER, reply_obj.reply or "")

        # 5) 事件 → 关系状态（§5.7 重构：判定在生成前已完成，这里只按行动结果计权）
        #    phase17：boundary 行动 → 「边界重申」事件；增量在 _commit_rel 经立场门控（T9）。
        if action is not None:
            ev = act17.action_event(action, ev_user,
                                    has_reply=bool((reply_obj.reply or "").strip()))
        else:
            ev = dict(ev_user) if ev_user else (
                {"event_type": "日常陪伴", "severity": 1, "importance": 1}
                if (reply_obj.reply or "").strip() else {})
        # 【§6.2 步骤 8】生成结果校验：违反行动决策 → 记 generation_violation，不改写
        if action is not None:
            try:
                v, vnote = act17.check_generation_violation(action, reply_obj.reply or "")
                if v:
                    action.generation_violation = v
                    action.violation_note = vnote
                    act17.mark_violation(self.conn, session_id, turn_a, vnote)
                    if self.verbose:
                        print(f"  [violation] {vnote}", flush=True)
            except Exception:
                pass
        ev_note = None
        if ev:
            ev["summary"] = f"{ev['event_type']}：{user_text[:14]} / {(reply_obj.reply or '')[:14]}"
            try:
                ev_note = self._note_event(day, session_id, ev, ev["summary"])
            except Exception as e:
                # 事件/关系状态落库失败，不应连累已经产生并落库的对话本身
                if self.verbose:
                    print(f"  [warn] 事件记录失败（对话已保存）: {str(e)[:80]}", flush=True)
                ev_note = {"event_type": ev["event_type"], "times": 0, "counted": False,
                           "note": f"{ev['event_type']}（本次事件未记录：{str(e)[:40]}）"}

        # 6) 消息驱动的时间推进（§5.9：回合计数，沉默计入回合）
        advanced = None
        if auto_day and self._rounds_in_session(session_id) >= AUTO_DAY_TURNS:
            advanced = self.advance_day(1)

        # Phase A：回复轮决策观测（prompt_digest + 状态前后快照）
        self._log_decision(session_id=session_id, day=day, turn_idx=turn_a,
                           decided=1, snapshot=snap, p_reply=willingness,
                           reply_mode=reply_obj.medium,
                           silent_reason=gate_reason,
                           prompt_digest=pdigest,
                           state_before=rel_before,
                           state_after=dict(self.rel_current or {}),
                           source="ported" if pf is not None else None)

        # §5.10：回复轮也落分支状态（情绪/近期消息/关系在 act() 里已变）
        self._save_state()

        return {"day": day, "user": user_text, "reply": reply_obj.reply or "",
                "medium": reply_obj.medium, "action": reply_obj.action,
                "sticker": sticker,
                "emotion": reply_obj.emotion, "event": ev, "event_note": ev_note,
                "rel": dict(self.rel_current or {}), "session_id": session_id,
                "advanced_to": advanced,
                "silent": False, "willingness": willingness}


# ---------------------------------------------------------------- 线管理
def _dial_lines(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT sim_id, branch_name, start_day, end_day, status, config_json, created_at "
        "FROM sim_runs ORDER BY created_at DESC").fetchall()
    out = []
    for sid, name, start, end, status, cfg_raw, created in rows:
        try:
            cfg = json.loads(cfg_raw or "{}")
        except Exception:
            cfg = {}
        if cfg.get("kind") != KIND:
            continue
        out.append({"sim_id": sid, "branch_name": name, "start_day": start, "end_day": end,
                    "status": status, "created_at": created, "cfg": cfg})
    return out


def _resolve_line(conn, ref: str) -> str:
    lines = _dial_lines(conn)
    if not lines:
        raise ValueError("还没有任何线，先执行 new")
    if ref in (None, "", "last"):
        return lines[0]["sim_id"]
    for ln in lines:
        if ln["sim_id"] == ref or ln["branch_name"] == ref:
            return ln["sim_id"]
    cands = [ln["sim_id"] for ln in lines if ref in ln["sim_id"] or ref in ln["branch_name"]]
    if len(cands) == 1:
        return cands[0]
    raise ValueError(f"无法唯一定位线: {ref}（候选 {len(cands)} 条）")


def new_line(conn, *, start_day: str, name: str, divergence: str | None = None,
             rewrite: str | None = None, rewrite_desc: str | None = None,
             seed: int = 0) -> str:
    """新建一条线（续聊线或 IF 分支线），并注册到 sim_runs + ifr_branch"""
    sim_id = pc.new_sim_id()
    divergence_point = divergence or start_day
    desc = rewrite_desc or (f"在 {divergence} 处改写：{rewrite}" if rewrite and divergence
                            else "在真实聊天记录基础上继续（未来线）")
    world = pc.build_world(conn, start_day, seed=seed, branch_name=name, branch_of="main",
                           divergence_point=divergence_point if rewrite else None,
                           divergence_desc=desc if rewrite else "",
                           rewritten_choice=rewrite, sim_id=sim_id)
    pc.init_sim(conn, world, n_rounds=0, config={
        "kind": KIND, "human": HUMAN, "partner": PARTNER,
        "auto_day_turns": AUTO_DAY_TURNS, "engine": "phase15_dial_engine v1",
        "start_day": start_day, "seed": seed,
        "divergence": {"point": divergence_point, "rewrite": rewrite} if rewrite else None,
        "run_state": {"day_start_rel": dict(world.rel_state or {}),
                      "day_events": [], "rel_current": dict(world.rel_state or {}),
                      "session_id": None, "session_day": None, "last_day": start_day},
    }, status="running")
    # 统一线索引（便于产品层列线；不影响 Phase 7 语义）
    conn.execute(
        "INSERT OR REPLACE INTO ifr_branch (branch_id, sim_id, parent_branch_id, "
        "divergence_point, rewritten_choice, divergence_desc, start_day, end_day, n_days, "
        "max_turns, pcc_mode, clock_seed, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)",
        (f"DIAL-{sim_id.split('-', 1)[1]}", sim_id, "main", divergence_point,
         rewrite, desc, start_day, 0, 0, "human-in-loop", seed, "running", pc.now_str()))
    conn.commit()
    # 时间上界放宽（对话线无预设窗口）
    world.clock.set_end("2099-12-31")
    return sim_id


# ---------------------------------------------------------------- 输出
def _fmt_rel(rel: dict | None) -> str:
    if not rel:
        return ""
    return "  ".join(f"{DIM_LABEL[d]}{rel.get(d, '—')}" for d in p6.DIMS if d in rel)


def _print_turn(res: dict) -> None:
    print(f"\n  你  {res['user']}")
    if res.get("silent"):
        w = res.get("willingness")
        ptxt = f"   [意愿 P={w:.2f}]" if isinstance(w, (int, float)) else ""
        print(f"  TA  （没回）{ptxt}")
    else:
        shown = res["reply"]
        if res["medium"] == "image":
            shown = f"[图片] {shown}".strip()
        elif res["medium"] == "emoji":
            shown = f"[表情包] {shown}".strip()
        elif res["medium"] == "voice":
            shown = f"[语音] {shown}".strip()
        print(f"  TA  {shown}" + (f"   （{res['action']}）" if res["action"] else ""))
    note = res.get("event_note")
    if note:
        suffix = "" if note.get("counted") else "  → 故五维不变（设计如此）"
        print(f"  ·  事件 {note['note']}{suffix}")
    if res.get("rel"):
        print(f"  ·  {_fmt_rel(res['rel'])}")


def _get_client_or_none(offline: bool):
    if offline:
        return StubClient(), None
    try:
        return get_client(), phase4_retrieval.MemoryRetriever()
    except Exception as e:
        print(f"[warn] 初始化 LLM/检索失败: {str(e)[:120]}")
        return None, None


# ---------------------------------------------------------------- 子命令
def cmd_new(args) -> None:
    conn = pc.connect()
    pc.apply_all_schemas(conn)          # 建齐结构（新库 / 空数据目录也能直接用）
    start = args.start or get_default_start(conn)
    sim_id = new_line(conn, start_day=start, name=args.name, divergence=args.divergence,
                      rewrite=args.rewrite, rewrite_desc=args.desc)
    print(f"已创建线「{args.name}」")
    print(f"  sim_id      : {sim_id}")
    print(f"  起点        : {start}")
    if args.rewrite:
        print(f"  改写点      : {args.divergence or start}")
        print(f"  改写内容    : {args.rewrite}")
    print(f"  下一步      : python app/phase15_dial_engine.py repl --line last")
    conn.close()


def cmd_say(args) -> None:
    conn = pc.connect()
    sim_id = _resolve_line(conn, args.line)
    client, retriever = _get_client_or_none(args.offline)
    if client is None:
        print("缺少 LLM API Key：请设置配置里 llm.api_key_env 指定的环境变量（默认 LLM_API_KEY），或用 --offline 自检")
        return
    eng = DialEngine(conn, sim_id, client=client, retriever=retriever)
    print(f"【{eng.branch_name}】{eng.world.day}")
    res = eng.say(args.text, auto_day=not args.no_auto_day)
    _print_turn(res)
    conn.close()


def cmd_repl(args) -> None:
    conn = pc.connect()
    sim_id = _resolve_line(conn, args.line)
    client, retriever = _get_client_or_none(args.offline)
    if client is None:
        print("缺少 LLM API Key：请设置配置里 llm.api_key_env 指定的环境变量（默认 LLM_API_KEY），或用 --offline 自检")
        return
    eng = DialEngine(conn, sim_id, client=client, retriever=retriever)
    print(f"=== {eng.branch_name}｜{eng.world.day} ===")
    print("直接输入消息回车发送；/day 推进一天，/show 看最近对话，/q 退出\n")
    while True:
        try:
            line = input("  你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/q", "/quit", "/exit"):
            break
        if line == "/day":
            eng.advance_day(1)
            continue
        if line == "/show":
            for day, sender, content in reversed(eng._recent_rows(20)):
                who = "你" if sender == HUMAN else "TA"
                print(f"  {day} {who}  {content}")
            continue
        try:
            _print_turn(eng.say(line))
        except Exception as e:
            print(f"  [error] {str(e)[:160]}")
    print("已保存。")
    conn.close()


def cmd_list(args) -> None:
    conn = pc.connect()
    lines = _dial_lines(conn)
    if not lines:
        print("还没有任何线。执行：python app/phase15_dial_engine.py new --name 名称")
    for ln in lines:
        n_msg = conn.execute("SELECT COUNT(*) FROM sim_messages WHERE sim_id=?",
                             (ln["sim_id"],)).fetchone()[0]
        div = (ln["cfg"].get("divergence") or {}).get("rewrite")
        print(f"- {ln['branch_name']}")
        print(f"    {ln['sim_id']}  起点 {ln['start_day']}  消息 {n_msg} 条  创建 {ln['created_at']}")
        if div:
            print(f"    改写: {div[:60]}")
    conn.close()


def cmd_show(args) -> None:
    conn = pc.connect()
    sim_id = _resolve_line(conn, args.line)
    rows = conn.execute(
        "SELECT day, sender, content, meta FROM sim_messages WHERE sim_id=? "
        "ORDER BY rowid ASC", (sim_id,)).fetchall()
    rows = rows[-args.tail:]
    cur_day = None
    for day, sender, content, meta in rows:
        if day != cur_day:
            print(f"\n──── {day} ────")
            cur_day = day
        who = "你" if sender == HUMAN else "TA"
        extra = ""
        try:
            m = json.loads(meta or "{}")
            if m.get("medium") and m["medium"] != "text":
                extra = f"[{m['medium']}] "
            if m.get("action"):
                extra += f"（{m['action']}）"
        except Exception:
            pass
        print(f"  {who}  {extra}{content}")
    rel = conn.execute(
        "SELECT day, closeness, conflict, trust, emotional_safety, comm_quality "
        "FROM sim_rel_state WHERE sim_id=? ORDER BY day DESC LIMIT 1", (sim_id,)).fetchone()
    if rel:
        print(f"\n  最新关系状态（{rel[0]}）: 亲密 {rel[1]}  冲突 {rel[2]}  信任 {rel[3]}  "
              f"情绪安全 {rel[4]}  沟通 {rel[5]}")
    conn.close()


def cmd_selftest(args) -> None:
    """离线自检：桩客户端验证全链路（世界→会话→回合→落库→关系更新→跨天），不需要 API key"""
    conn = pc.connect()
    pc.apply_all_schemas(conn)          # 空数据目录也必须能跑：先建齐全部结构
    name = f"SELFTEST-{time.strftime('%m%d-%H%M%S')}"
    start = args.start or get_default_start(conn)
    sim_id = new_line(conn, start_day=start, name=name, divergence=args.divergence,
                      rewrite=args.rewrite)
    print(f"[1] 建线 OK  sim_id={sim_id}")
    eng = DialEngine(conn, sim_id, client=StubClient(), retriever=None)
    print(f"[2] 世界 OK  起点={eng.start_day}  当日真实关系状态="
          f"{_fmt_rel(eng.world.rel_state)}")
    before = {
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
    }
    for i, txt in enumerate(["在忙吗", "今天好累啊", "我们是不是很久没见了", "对不起"],
                            start=1):
        res = eng.say(txt)
        acted = "（没回·意愿判定）" if res.get("silent") else f"回「{res['reply']}」"
        print(f"[3.{i}] 回合 OK  {res['day']}  TA {acted}  事件="
              f"{(res.get('event') or {}).get('event_type', '—')}  亲密="
              f"{(res.get('rel') or {}).get('closeness')}")
    n_msg = conn.execute("SELECT COUNT(*) FROM sim_messages WHERE sim_id=?",
                         (sim_id,)).fetchone()[0]
    n_ev = conn.execute("SELECT COUNT(*) FROM sim_events WHERE sim_id=?",
                        (sim_id,)).fetchone()[0]
    print(f"[4] 落库 OK  sim_messages={n_msg}  sim_events={n_ev}")
    after = {
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
    }
    print(f"[5] 主库隔离 {'OK' if before == after else '失败!!'}  前={before} 后={after}")
    # 跨天
    res = eng.advance_day(1)
    total = conn.execute("SELECT COUNT(*) FROM sim_messages WHERE session_id=?",
                         (eng.run_state["session_id"] or "",)).fetchone()[0]
    eng2 = DialEngine(conn, sim_id, client=StubClient(), retriever=None)
    print(f"[6] 跨天 OK  新日={eng2.world.day}  缓冲重建={eng2.wm.entry_count} 条  续聊前 "
          f"session 消息={total}")
    res2 = eng2.say("昨天没说完的话")
    print(f"[7] 重启续聊 OK  TA 回「{res2['reply']}」")
    # 清理自检线（幂等：删掉本次 selftest 产物，避免污染线列表）
    for t in ("sim_messages", "sim_sessions", "sim_events", "sim_facts", "sim_rel_state",
              "sim_pcc_log", "sim_working_mem", "sim_decision_log",
              "sim_branch_state", "sim_action_log"):
        conn.execute(f"DELETE FROM {t} WHERE sim_id=?", (sim_id,))
    conn.execute("DELETE FROM ifr_branch WHERE sim_id=?", (sim_id,))
    conn.execute("DELETE FROM sim_runs WHERE sim_id=?", (sim_id,))
    conn.commit()
    print("[8] 自检线已清理 OK")
    print("\n全部通过。下一步（需要 LLM API Key）："
          "\n  python app/phase15_dial_engine.py new --name 继续聊"
          "\n  python app/phase15_dial_engine.py repl --line last")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="IfWe Phase 15 对话内核（你上场，与对方对话）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="新建一条线（续聊线 / IF 分支线）")
    p.add_argument("--start", default=None, help="起点日（默认=最后一条消息的次日）")
    p.add_argument("--name", default=None, help="线的名字")
    p.add_argument("--divergence", default=None, help="改写点（IF 线用）")
    p.add_argument("--rewrite", default=None, help="你当时做出的不同选择（一句话）")
    p.add_argument("--desc", default=None, help="改写描述（可选）")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("say", help="说一句话")
    p.add_argument("text")
    p.add_argument("--line", default="last")
    p.add_argument("--offline", action="store_true", help="用桩客户端（不联网）")
    p.add_argument("--no-auto-day", action="store_true", help="不自动跨天")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("repl", help="交互式连续聊天")
    p.add_argument("--line", default="last")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_repl)

    p = sub.add_parser("list", help="列出所有线")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="打印一条线的对话")
    p.add_argument("--line", default="last")
    p.add_argument("--tail", type=int, default=40)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("selftest", help="离线全链路自检（不需要 API key）")
    p.add_argument("--start", default=None, help="起点日（默认=最后一条消息的次日）")
    p.add_argument("--divergence", default=None)
    p.add_argument("--rewrite", default=None)
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    if args.cmd == "new" and not args.name:
        args.name = args.rewrite[:14] if args.rewrite else "继续聊"
    args.func(args)


if __name__ == "__main__":
    main()
