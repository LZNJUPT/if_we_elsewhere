# -*- coding: utf-8 -*-
"""Phase 17 · 当前立场 / 本轮场景 / 关系增量门控（二期方案 v2 §6.1C/D + §5.8）

设计红线（§14）：
  - 不把「某日期 + 某关键词」写死成特殊分支；
  - 不用「出现某个词就冷淡」的关键词表做情境冷淡判断；
  - 一切立场判断只消费【数据侧证据】：
      1) data/decision_points.json 的 stance_evidence 结构化标注（用户确认的真实决策点）；
      2) turning_points 表的 gap 型拐点（断联窗口，真实数据）；
      3) P_anchor（锚定后的真实回复率，persona_fidelity paired 口径）。
  - 代码里出现的分档切点全部注明数据出处；改证据 = 改数据文件，不改代码。

MVP 边界（诚实声明）：
  - 本模块是「立场/场景 MVP」：结构、证据链、门控通道全部落地；
    阶段模型 / 变化点检测 / 留出验证属 Phase D（§7.2），届时替换这里的分档推断。
  - rel_delta 门控只保证「拒绝立场/极低联系档下，普通话题不再自动加热」；
    m 调制（裁定三）仍恒 1.0，档位取值待 Phase D 校准。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 数据出处
# contact_willingness 分档切点：真实各期 paired 回复率（方案 §3.2 / BASELINE_v1.md）：
#   沉默段 0.000 ｜ 冷淡期 0.146 ｜ 升温期 0.572 ｜ 冲突期 0.667
# 取相邻期中点作切点（数据推导，非拍脑袋）：
#   <0.073=极低（沉默段~冷淡期之间中点 0.073）；<0.359=低（冷淡~升温中点）；
#   <0.620=中（升温~冲突中点）；>=0.620=高
P_BANDS = [
    (0.073, "极低"),
    (0.359, "低"),
    (0.620, "中"),
    (1.01, "高"),
]

# 正向维度（closeness/trust/emotional_safety/comm_quality 的正增量）；conflict 维单独处理
POSITIVE_DIMS = ("closeness", "trust", "emotional_safety", "comm_quality")

STANCE_VERSION = "stance_mvp_v1"


def _load_evidence() -> list[dict]:
    """读决策点证据（data/decision_points.json）；缺失/损坏 → 空列表（不臆测）"""
    for base in (ROOT, ROOT / "app"):
        f = base / "data" / "decision_points.json"
        if f.exists():
            try:
                dps = json.loads(f.read_text(encoding="utf-8")).get("decision_points")
                if isinstance(dps, list):
                    return [d for d in dps if isinstance(d, dict)]
            except Exception:
                pass
    # 发布树 data 目录在 DATA_DIR（demo 隔离）
    try:
        import config as cfg_mod                      # 发布树路线
        f = Path(cfg_mod.data_dir()) / "decision_points.json"
        if f.exists():
            dps = json.loads(f.read_text(encoding="utf-8")).get("decision_points")
            if isinstance(dps, list):
                return [d for d in dps if isinstance(d, dict)]
    except Exception:
        pass
    return []


def band_of(p: float) -> str:
    for hi, name in P_BANDS:
        if p < hi:
            return name
    return "高"


def infer_stance(conn: sqlite3.Connection, start_day: str,
                 p_anchor: float | None = None) -> dict:
    """从数据侧证据推断分支起点处的当前立场（§6.1C；全部证据可溯源）。

    优先级：
      1. 分支起点前最新的 stance_evidence 标注（用户确认的真实决策点）；
      2. turning_points 的 gap 拐点（起点前的最近断联 → 低联系信号）；
      3. P_anchor 分档 → contact_willingness。
    无拒绝证据时 romantic_intent=未表态（不臆测）。
    """
    st = {
        "relationship_status": "未明确",
        "romantic_intent": "未表态",
        "contact_willingness": None,
        "initiation_willingness": None,
        "topic_scope": None,
        "boundary_strength": None,
        "boundary_consequence": None,
        "affect_toward_partner": None,
        "confidence": 0.3,
        "evidence_ids": [],
        "version": STANCE_VERSION,
    }
    evidence_used = False
    for dp in _load_evidence():
        if dp.get("day") and dp["day"] <= start_day and dp.get("stance_evidence"):
            ev = dp["stance_evidence"]
            for k in ("relationship_status", "romantic_intent", "topic_scope",
                      "boundary_strength", "boundary_consequence",
                      "affect_toward_partner"):
                if ev.get(k):
                    st[k] = ev[k]
            if ev.get("romantic_intent"):
                st["romantic_intent"] = ev["romantic_intent"]
            if ev.get("contact_willingness"):
                st["contact_willingness"] = ev["contact_willingness"]
            if ev.get("initiation_willingness"):
                st["initiation_willingness"] = ev["initiation_willingness"]
            st["evidence_ids"].append(dp.get("id") or dp.get("day"))
            evidence_used = True
            st["confidence"] = 0.7
            break                      # 取起点前最新一条
    # turning_points 的 gap 拐点（真实数据；不写日期）
    try:
        row = conn.execute(
            "SELECT tp_id, day FROM turning_points "
            "WHERE tp_type='gap' AND day<=? ORDER BY day DESC LIMIT 1",
            (start_day,)).fetchone()
        if row:
            st["evidence_ids"].append(row[0])
            if st["initiation_willingness"] is None:
                st["initiation_willingness"] = "低"     # 起点前最近状态是断联
    except sqlite3.Error:
        pass
    # P_anchor 分档（paired 口径，锚定值）
    if p_anchor is not None:
        band = band_of(float(p_anchor))
        st["p_anchor"] = round(float(p_anchor), 4)
        st["p_band"] = band
        if st["contact_willingness"] is None:
            st["contact_willingness"] = {"极低": "被动极低", "低": "被动低频",
                                         "中": "有来有回", "高": "主动且有来有回"}[band]
    if not evidence_used and p_anchor is None:
        st["confidence"] = 0.2
    return st


def stance_gate(stance: dict) -> dict:
    """关系增量门控（§5.8：普通话题不得在拒绝立场下自动加热）。

    返回 {event_type: {dim: gate}} 的计算规则说明（实现见 gated_delta）：
      - 正向维增量：romantic_intent=明确拒绝、或 P 档=极低 → 0.0（普通话题不加热）；
        其余（含未表态+非极低档）→ 1.0（MVP 不引入未经校准的中间档，Phase D 校准）；
      - conflict 维的正增量（冲突恶化）：任何立场下照常 → 1.0（冲突仍疼，不因立场豁免）。
    """
    intent = stance.get("romantic_intent")
    band = stance.get("p_band") or ""
    pos_gate = 0.0 if (intent == "明确拒绝" or band == "极低") else 1.0
    return {"pos": pos_gate, "conflict": 1.0}


def gated_delta(stance: dict, ev_delta: dict) -> dict:
    """按立场门控一条事件增量（phase17 模式下 _commit_rel 的事件通道）。"""
    g = stance_gate(stance)
    out = {}
    for dim, v in (ev_delta or {}).items():
        if dim == "conflict":
            out[dim] = round(v * g["conflict"], 5)
        elif v > 0:
            out[dim] = round(v * g["pos"], 5)
        else:
            out[dim] = round(v, 5)          # 事件自带的负增量（如冲突连带削弱）照常
    return out


# ---------------------------------------------------------------- SceneState
# 关系推进检测词：与 phase5_a2_loop.REWRITE_KEYWORDS 同源（既有坐标系，勿新造）。
# 用途限定：识别「本轮是否触碰关系边界/推进话题」——冷淡与否由立场决定，词表只管话题分类。
ADVANCE_LEXICON = ["复合", "试试", "先当朋友", "机会", "愿意", "答应",
                   "慢慢来", "重新", "接受", "和好", "在一起"]
# 元对话最小检测（§8 P3 完整方案在 Phase D 替换；只识别「B 是否真实」类提问）
META_LEXICON = ["是不是机器人", "是真人吗", "是ai吗", "是 AI 吗", "人工智能", "程序",
                "数字分身", "你是真人"]


# ---------------------------------------------------------------- 世界契约（§8.1，Phase D 落常量）
# §8.1 硬要求：实现前必须固定并写进文档。3 档 epistemic + 元叙述政策。
WORLD_CONTRACT = {
    "epistemic_levels": ["不知道", "怀疑", "知道"],
    "epistemic_default": "不知道",           # MVP 缺省：B 不知道自己是数字孪生
    "meta_narrative_policy": "用户的元叙述是对话内容，不自动升级为系统指令",
    "assistant_fallback": "B 不因用户说『你是模型』就切换为通用助手",
    "stance_persistence": "即使 B 知道自己是模型，当前关系立场继续有效",
}


def make_meta_detector(client=None):
    """元对话判定器工厂（§8.2：结构化识别，不是关键词表）。

    - client（LLM 结构化客户端）提供时：用 phase5_llm.SceneMeta 判定——正式路径；
    - client 为 None（离线/无 Key）：返回 None，infer_scene 降级到 Phase C 的
      最小词表检测（仅为不阻断；§8.2 禁止词表是【正式方案】，降级路径在场景里
      如实标注 scene_source=fallback_lexicon，待标注数据校准后移除）。
    标注集：data/meta_dialogue_labels.json（骨架已建，待人工标注后校准）。
    """
    if client is None:
        return None

    def detect(user_text: str, buffer_text: str = "") -> dict | None:
        try:
            import phase5_llm as llm
            obj = client.extract(system=llm.META_SYS,
                                 user=llm.meta_user_block(user_text, buffer_text),
                                 response_model=llm.SceneMeta)
            return {"is_meta": bool(obj.is_meta),
                    "epistemic": obj.epistemic_level if obj.epistemic_level in
                    WORLD_CONTRACT["epistemic_levels"] else WORLD_CONTRACT["epistemic_default"],
                    "is_system_instruction": bool(obj.is_system_instruction),
                    "note": obj.note, "source": "llm"}
        except Exception:
            return None
    return detect


def infer_scene(user_text: str, ev_user: dict, stance: dict,
                meta_detector=None) -> dict:
    """本轮场景（§6.1D）：只看用户消息 + 事件分类 + 立场；零 token（除注入的判定器）。"""
    text = (user_text or "").strip()
    low = text.lower()
    ev_type = (ev_user or {}).get("event_type")
    scene = {
        "scene_mode": "普通话题",
        "epistemic_contract": WORLD_CONTRACT["epistemic_default"],
        "user_intent_summary": (text[:40] or None),
        "relationship_relevance": "low",
        "boundary_pressure": "low",
        "confidence": 0.5,
        "evidence_ids": [],
        "scene_source": "heuristic",
    }
    # 元对话：结构化判定器优先（§8.2 正式路径）；降级词表仅作 LLM 不可用时的兜底
    meta = None
    if meta_detector is not None:
        try:
            meta = meta_detector(text)
        except Exception:
            meta = None
    if meta is not None:
        if meta.get("is_meta"):
            scene["scene_mode"] = "元对话"
            scene["epistemic_contract"] = meta.get("epistemic") \
                or WORLD_CONTRACT["epistemic_default"]
            scene["scene_source"] = "llm_structured"
            scene["confidence"] = 0.7
            return scene
        scene["scene_source"] = "llm_structured"      # 判定过且非元对话
    elif any(k in low for k in META_LEXICON):
        scene["scene_mode"] = "元对话"
        scene["scene_source"] = "fallback_lexicon"    # §8.2 降级路径，待校准移除
        scene["confidence"] = 0.4
        return scene
    advance = any(k in text for k in ADVANCE_LEXICON)
    if ev_type in ("冲突/分歧",):
        scene["scene_mode"] = "冲突"
        scene["relationship_relevance"] = "high"
        scene["boundary_pressure"] = "medium"
    elif advance or ev_type in ("纪念日/承诺", "冲突修复/和好"):
        scene["scene_mode"] = "关系话题"
        scene["relationship_relevance"] = "high"
        if stance.get("romantic_intent") == "明确拒绝" or \
                stance.get("contact_willingness") in ("被动极低", "被动低频"):
            scene["boundary_pressure"] = "high"
    elif ev_type in ("学业考试/工作求职", "金钱/转账", "健康"):
        scene["scene_mode"] = "事务咨询"
    return scene
