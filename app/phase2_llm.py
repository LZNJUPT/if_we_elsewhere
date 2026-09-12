# -*- coding: utf-8 -*-
"""
IfWe · LLM 结构化输出鲁棒层
参考 poc/graphiti_poc.py 的 DashScopeRepairClient（类型修复+properties 解包+多层重试），
改为独立同步实现（不依赖 graphiti_core）。

原则：
- 只发送 content_clean 文本；不把原文/隐私发给 API
- json_object 模式 + pydantic 校验 + 字段级类型修复；偶发空响应/截断 → 多层重试
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

import config as cfg_mod

# OpenAI 兼容接入点：默认 DeepSeek；config llm.base_url/model 可改，
# 环境变量 IFWE_LLM_BASE_URL / IFWE_LLM_MODEL 优先级更高
_llm_cfg = cfg_mod.load()["llm"]
BASE_URL = os.environ.get("IFWE_LLM_BASE_URL") or _llm_cfg["base_url"]
MODEL = os.environ.get("IFWE_LLM_MODEL") or _llm_cfg["model"]
logger = logging.getLogger("ifwe.m4")

# 事件类型本体（评审稿 B3，D5 已锁定草案）
EVENT_TYPES = [
    "见面/出行", "纪念日/承诺", "冲突/分歧", "冲突修复/和好", "情绪低谷/安慰",
    "高兴/庆祝", "学业考试/工作求职", "家人/朋友", "健康", "金钱/转账",
    "娱乐互动", "日常陪伴",
]
ENTITY_CATEGORIES = ["person", "pet", "place", "study", "work", "family", "leisure", "other"]


class ExtractionEvent(BaseModel):
    event_type: str = Field("", description="事件类型，应属于本体")
    summary: str = Field("", description="≤30字中文摘要，不含隐私")
    severity: int = Field(0, description="负面/冲突/情绪波动强度 1-5")
    importance: int = Field(0, description="对关系长期影响 1-5")


class ExtractionEntity(BaseModel):
    name: str = Field(..., description="实体名(人物/宠物/地点/学校/项目等)")
    category: str = Field("other", description="类别")
    note: str = Field("", description="一句话补充(与双方的关系/上下文)≤40字")


class SessionExtraction(BaseModel):
    topic: str = Field("", description="一句话主题 ≤20字")
    events: list[ExtractionEvent] = []
    entities: list[ExtractionEntity] = []
    notable: str = Field("", description="值得注意的互动细节 ≤40字(无则空串)")


# ---------- 类型修复工具（移植自 graphiti_poc.py） ----------
def _is_model(t) -> bool:
    return isinstance(t, type) and issubclass(t, BaseModel) if isinstance(t, type) else False


def _unwrap_model_wrap(data: Any, model) -> Any:
    """DeepSeek 偶尔把 schema 再包一层 properties/root 等键，解包到真实字段"""
    if model is None or not isinstance(data, dict):
        return data
    field_names = set(getattr(model, "model_fields", {}).keys())
    if not field_names or field_names.intersection(data):
        return data
    for key in ("properties", "schema", "json_schema", "root", "result", "output", "data"):
        v = data.get(key)
        if isinstance(v, dict) and field_names.intersection(v):
            return v
    return data


def _coerce_scalar(s: Any, typ) -> Any:
    origin = getattr(typ, "__origin__", typ)
    if origin in (list, tuple, set):
        if isinstance(s, (list, tuple, set)):
            return list(s)
        if isinstance(s, str) and s.strip().startswith("["):
            try:
                return json.loads(s.strip())
            except Exception:
                return s
        return [s]
    if origin is dict:
        if isinstance(s, dict):
            return dict(s)
        if isinstance(s, str) and s.strip().startswith("{"):
            try:
                return json.loads(s.strip())
            except Exception:
                return s
        return s
    if isinstance(s, str):
        if isinstance(typ, type):
            if issubclass(typ, bool):
                return s.strip().lower() in ("true", "1", "yes")
            if issubclass(typ, int):
                st = s.strip()
                try:
                    return int(st) if st.replace(".", "", 1).isdigit() else 0
                except Exception:
                    return 0
            if issubclass(typ, float):
                try:
                    return float(s)
                except Exception:
                    return 0.0
    return s


def _repair_json_types(data: Any, model) -> Any:
    """按 response_model 字段类型递归修复 LLM 输出（数组/对象/标量错位、null 剔除）"""
    if model is None or not isinstance(data, dict):
        return data
    fields = getattr(model, "model_fields", {})
    for name, f in fields.items():
        if name not in data:
            continue
        typ = f.annotation
        origin = getattr(typ, "__origin__", typ)
        args = getattr(typ, "__args__", ())
        val = data[name]
        if origin in (list, tuple, set):
            inner = args[0] if args else None
            if isinstance(val, list):
                items = []
                for v in val:
                    if v is None:
                        continue
                    items.append(_repair_json_types(v, inner) if _is_model(inner) else _coerce_scalar(v, inner))
                data[name] = items
            elif isinstance(val, str) and val.strip().startswith("["):
                try:
                    data[name] = json.loads(val)
                except Exception:
                    data[name] = []
            else:
                data[name] = [] if val is None else [val]
        elif origin is dict:
            data[name] = data[name] if isinstance(val, dict) else {}
        elif _is_model(typ):
            data[name] = _repair_json_types(val, typ)
        else:
            data[name] = _coerce_scalar(val, typ)
    return data


def _extract_json(raw: str) -> Any:
    """从模型输出中提取 JSON：优先整体，失败则找代码块/最外层大括号"""
    s = raw.strip()
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            s = m.group(1)
    # 找到首个 { 到末个 } 之间内容
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    raise ValueError(f"无法解析 JSON: {raw[:120]!r}...")


_ALIAS = {
    "topic": ("main_topic", "topic_summary", "summary_topic", "一句话主题"),
    "notable": ("note", "观察", "互动细节"),
    "event_type": ("type", "event", "eventType", "事件类型"),
    "summary": ("desc", "描述", "details"),
    "category": ("entity_type", "entityType"),
}


def _rename_keys(d: dict, target: str) -> None:
    for alias in _ALIAS.get(target, ()):
        if alias in d and target not in d:
            d[target] = d.pop(alias)


def _normalize_extraction(data: Any) -> dict:
    """键名归一化 + 缺省填充：模型输出的键可能与 schema 不同(type/事件类型…)"""
    if not isinstance(data, dict):
        return {}
    _rename_keys(data, "topic")
    _rename_keys(data, "notable")
    data.setdefault("topic", "")
    data.setdefault("notable", "")
    events = data.get("events", [])
    if isinstance(events, dict):
        events = list(events.values())
    if not isinstance(events, list):
        events = []
    evs = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        _rename_keys(ev, "event_type")
        _rename_keys(ev, "summary")
        ev.setdefault("severity", 0)
        ev.setdefault("importance", 0)
        ev.setdefault("summary", "")
        ev.setdefault("event_type", "")
        evs.append(ev)
    data["events"] = evs
    entities = data.get("entities", [])
    if isinstance(entities, dict):
        entities = list(entities.values())
    if not isinstance(entities, list):
        entities = []
    ents = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        _rename_keys(ent, "category")
        ent.setdefault("category", "other")
        ent.setdefault("note", "")
        ents.append(ent)
    data["entities"] = ents
    return data


class DeepSeekRepairClient:
    """OpenAI 兼容(DeepSeek) + json_object + 类型修复 + 多层重试"""

    def __init__(self, api_key: Optional[str] = None, max_tokens: int = 4096, max_attempts: int = 3):
        from openai import OpenAI

        self.api_key = api_key or cfg_mod.api_key()
        if not self.api_key:
            env_name = cfg_mod.load()["llm"]["api_key_env"]
            raise RuntimeError(f"缺少 LLM API Key（环境变量 {env_name}）")
        self.client = OpenAI(api_key=self.api_key, base_url=BASE_URL)
        self.model = MODEL
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        self.last_usage: Optional[dict] = None

    def extract(self, system: str, user: str, response_model=SessionExtraction):
        """调用 + 修复 + 校验，返回 pydantic 对象"""
        last_err = None
        for attempt in range(self.max_attempts):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    temperature=0,
                    max_tokens=self.max_tokens,
                    response_format={"type": "json_object"},
                )
                u = getattr(resp, "usage", None)
                if u is not None:
                    self.last_usage = {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens}
                raw = (resp.choices[0].message.content or "").strip()
                if not raw:
                    raise ValueError("空响应")
                data = _extract_json(raw)
                data = _unwrap_model_wrap(data, response_model)
                data = _normalize_extraction(data)
                data = _repair_json_types(data, response_model)
                obj = response_model.model_validate(data)
                return obj
            except Exception as e:
                last_err = e
                logger.warning("  抽取重试 %d/%d 失败(%s): %s",
                               attempt + 1, self.max_attempts, type(e).__name__, str(e)[:120])
                time.sleep(2 + attempt * 2)
        raise last_err  # type: ignore[misc]


SYSTEM_PROMPT = f"""你是 IfWe 项目的关系分析助手。下面的内容是两位当事人——A 与 B——某段私聊文字的摘录（已脱敏）。请从中抽取重要事件与关键实体。

要求：
1. 只依据给出的内容判断，严禁编造。
2. 事件类型只能从本体中选一个；一个会话通常可抽 0~3 个事件。宁可稍多抽，也不要漏掉对关系进展有意义的互动：商量见面/出行、纪念日/承诺、矛盾与分歧、和好/道歉、情绪低落与安慰、开心庆祝、考试求职进度、家人朋友动态、健康、金钱往来、一起打游戏/玩小程序、日常陪伴细节等。
3. 每条事件写 ≤30 字中文摘要（概述谁、做了什么、结果/情绪），不得出现手机号/地址/证件号等任何隐私信息。
4. severity=该事件的负面/冲突/情绪波动强度(1-5)；importance=对两人关系长期影响(1-5)。
5. 实体：类别 ∈ {ENTITY_CATEGORIES}，只抽反复出现或对关系有意义的实体（人名/宠物名/城市地点/学校/公司/项目/家庭角色/共同爱好）。
6. 输出必须只含合法 JSON，无多余文字。

事件类型本体: {EVENT_TYPES}"""


def build_session_prompt(session: dict, msgs: list[dict], max_chars: int = 8000) -> str:
    """会话 -> 用户 prompt。msgs 为该会话消息(含 sender/text/ts)，只渲染收录文本行。"""
    dt0 = time.strftime("%Y-%m-%d %H:%M", time.localtime(session["start_ts"]))
    dt1 = time.strftime("%H:%M", time.localtime(session["end_ts"]))
    lines = [f"会话时间: {dt0}~{dt1}"]
    used = 0
    for m in msgs:
        t = (m["text"] or "")
        if m["subtype"] not in ("text", "quote_text") or not t.strip() or m["privacy"]:
            continue
        tt = time.strftime("%H:%M", time.localtime(m["ts"]))
        line = f"[{tt} {m['sender']}] {t}"
        if used + len(line) > max_chars:
            lines.append("[…本条会话过长，已截断…]")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)