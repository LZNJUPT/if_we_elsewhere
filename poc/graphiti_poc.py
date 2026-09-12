# -*- coding: utf-8 -*-
"""
Graphiti PoC for IfWe
- 数据: 私聊 JSONL (微信私聊双人)
- 后端: Kuzu(embedded, 无服务器)  说明: KuzuDriver 在 Graphiti 中已标记 deprecated,
        本 PoC 用它验证"内存/嵌入式"可行性与中文语料抽取质量; 结论章节单独评估.
- LLM/Embedding: 通义千问 DashScope(兼容 OpenAI 端点)
- 只读数据, 不修改原始 JSONL; 图库写入 poc/data/kuzu_chat
"""
import asyncio
import json
import logging
import os
import time
import warnings
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

# ---- 环境预置(必须在 import graphiti_core 之前) ----
os.environ.setdefault("EMBEDDING_DIM", "512")          # bge-small-zh 维度
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # HuggingFace 镜像(国内)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

BASE_URL = "https://api.deepseek.com"
LLM_MODEL = os.environ.get("IFWE_LLM_MODEL", "deepseek-chat")
LOCAL_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
LOCAL_EMBED_DIM = 512

from graphiti_core import Graphiti
from graphiti_core.cross_encoder import OpenAIRerankerClient
from graphiti_core.driver.kuzu_driver import KuzuDriver
from graphiti_core.embedder.client import EmbedderClient, EmbedderConfig
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import EpisodeType


class FastEmbedLocal(EmbedderClient):
    """本地 embedding(fastembed + bge 系列), 离线免费; create_batch 兼容 Graphiti"""

    def __init__(self, model_name: str = LOCAL_EMBED_MODEL, embedding_dim: int = LOCAL_EMBED_DIM):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name)
        self.config = EmbedderConfig(embedding_dim=embedding_dim)

    async def create(self, input_data):
        if isinstance(input_data, str):
            input_data = [input_data]
        embs = list(self._model.embed(input_data))
        return [float(x) for x in embs[0]][: self.config.embedding_dim]

    async def create_batch(self, input_data_list):
        embs = list(self._model.embed(input_data_list))
        return [[float(x) for x in e][: self.config.embedding_dim] for e in embs]

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ifwe_poc")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="graphiti_core")
warnings.filterwarnings("ignore", category=FutureWarning)

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
# 数据源走 config chat.source 的默认位置（或环境变量 IFWE_CHAT_SOURCE 覆盖）
DATA = ROOT / os.environ.get("IFWE_CHAT_SOURCE", "data/raw/chat.jsonl")
DB_DIR = Path(__file__).resolve().parent / "data" / "kuzu_chat"
TZ = timezone(timedelta(hours=8))

SESSION_GAP = 30 * 60      # 同一次会话: 相邻消息间隔 <= 30 分钟
EARLY_MSGS = 150           # 早期样本累计文本消息数(DeepSeek 有重试损耗, 保持可控)
LATE_MSGS = 60             # 近期样本累计文本消息数


def _is_model(t):
    return isinstance(t, type) and issubclass(t, BaseModel) if isinstance(t, type) else False


def _coerce_scalar(s, typ):
    """把模型误回成标量/错误类型字符串的字段值还原(qwen json_object 常见问题)"""
    origin = getattr(typ, "__origin__", typ)
    args = getattr(typ, "__args__", ())
    if origin in (list, tuple, set):
        # 期望数组但拿到标量: 包装成单元素数组(qwen 常见)
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
            if issubclass(typ, BaseModel):
                return s
            if issubclass(typ, bool):
                return s.strip().lower() in ("true", "1", "yes")
            if issubclass(typ, int):
                st = s.strip()
                try:
                    return int(st) if st.replace(".", "", 1).isdigit() else s
                except Exception:
                    return s
            if issubclass(typ, float):
                try:
                    return float(s)
                except Exception:
                    return s
    return s


def _unwrap_model_wrap(data, model):
    """DeepSeek 有时会把 schema 再包一层 properties/schema/root 等键, 解包到真实字段"""
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


def _repair_json_types(data, model):
    """按 response_model 字段类型递归修复 LLM 输出中的类型错位"""
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
                        continue  # qwen 常用 null 填充未知索引, 直接剔除
                    if _is_model(inner):
                        items.append(_repair_json_types(v, inner))
                    elif inner is not None and getattr(inner, "__name__", None) not in ("Any",):
                        items.append(_coerce_scalar(v, inner))
                    else:
                        items.append(v)
                data[name] = items
            elif isinstance(val, str) and val.strip().startswith("["):
                try:
                    data[name] = json.loads(val)
                except Exception:
                    data[name] = [val]
            else:
                data[name] = [val]  # 标量 -> 单元素数组
        elif origin is dict:
            inner = args[1] if len(args) >= 2 else None
            if isinstance(val, dict):
                data[name] = {
                    k: _repair_json_types(v, inner) if _is_model(inner) else _coerce_scalar(v, inner)
                    for k, v in val.items()
                }
            else:
                data[name] = _coerce_scalar(val, typ)
        elif _is_model(typ):
            data[name] = _repair_json_types(val, typ)
        else:
            data[name] = _coerce_scalar(val, typ)
    return data


class DashScopeRepairClient(OpenAIGenericClient):
    """OpenAI 兼容(qwen) + 输出类型修复: json_object 模式下 qwen 常把数组/对象回成字符串"""

    async def generate_response(
        self,
        messages,
        response_model=None,
        max_tokens=None,
        model_size=ModelSize.medium,
        group_id=None,
        prompt_name=None,
        *,
        attribute_extraction=False,
    ):
        data = None
        last_err = None
        for attempt in range(6):  # DeepSeek 偶发空响应/截断 JSON, 在 graphiti 内建重试外再加一层
            try:
                data = await super().generate_response(
                    messages,
                    response_model,
                    max_tokens=min(max_tokens or 8000, 8000),  # DeepSeek 侧按需下调
                    model_size=model_size,
                    group_id=group_id,
                    prompt_name=prompt_name,
                    attribute_extraction=attribute_extraction,
                )
                break
            except Exception as e:
                last_err = e
                wait = 2 + attempt * 2
                logger.warning("wrapper 重试 %d 次后失败(%s), %.0fs 后重试: %s",
                               attempt + 1, type(e).__name__, wait, str(e)[:120])
                await asyncio.sleep(wait)
        else:
            raise last_err  # type: ignore[misc]
        data = _unwrap_model_wrap(data, response_model)
        return _repair_json_types(data, response_model)


def load_text_messages():
    """读取文本消息(type=0), 返回 [(timestamp, accountName, content)]"""
    msgs = []
    with open(DATA, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("_type") != "message":
                continue
            if rec.get("type") != 0:
                continue
            content = (rec.get("content") or "").strip()
            if not content:
                continue
            msgs.append((rec["timestamp"], rec.get("accountName", ""), content))
    msgs.sort(key=lambda x: x[0])
    return msgs


def to_sessions(msgs):
    """按时间间隔切分会话"""
    sessions = []
    cur = []
    prev_ts = None
    for ts, name, content in msgs:
        if prev_ts is not None and ts - prev_ts > SESSION_GAP:
            if len(cur) >= 2:
                sessions.append(cur)
            cur = []
        cur.append((ts, name, content))
        prev_ts = ts
    if len(cur) >= 2:
        sessions.append(cur)
    return sessions


def session_body(session):
    """会话 -> episode 文本(带说话人与时间)"""
    lines = []
    for ts, name, content in session:
        dt = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{dt}] {name}: {content}")
    return "\n".join(lines)


async def ensure_fts(driver):
    """Kuzu 后端有 FTS 索引创建漂移 bug(0.29.3 vs kuzu 0.11.3), 显式补建, 已存在则忽略"""
    fts = [
        ("Episodic", "episode_content", ["content", "source", "source_description"]),
        ("Entity", "node_name_and_summary", ["name", "summary"]),
        ("Community", "community_name", ["name"]),
        ("RelatesToNode_", "edge_name_and_fact", ["name", "fact"]),
    ]
    for tbl, idx, cols in fts:
        try:
            await driver.execute_query(f"CALL CREATE_FTS_INDEX('{tbl}', '{idx}', {cols!r})")
            logger.warning("FTS created(manual): %s", idx)
        except Exception as e:
            logger.warning("FTS skip(exists?): %s (%s)", idx, str(e)[:120])


async def semantic_search(embedder, driver, query, k=6):
    """graphiti.search 的语义检索回退(绕过 Kuzu FTS 兼容问题), 基于关系边 fact_embedding 余弦排序"""
    import numpy as np

    q_emb = await embedder.create(query)
    q = np.array(q_emb, dtype=float)
    r, _, _ = await driver.execute_query(
        "MATCH (e:RelatesToNode_) "
        "RETURN e.name AS name, e.fact AS fact, e.fact_embedding AS emb, "
        "e.valid_at AS valid_at, e.invalid_at AS invalid_at"
    )
    scored = []
    for row in r:
        emb = row.get("emb")
        if not emb:
            continue
        e = np.array(emb, dtype=float)
        denom = (np.linalg.norm(q) * np.linalg.norm(e)) or 1.0
        scored.append((float(np.dot(q, e) / denom), row))
    scored.sort(key=lambda x: x[0], reverse=True)
    print(f"\n### Q(semantic fallback): {query}")
    for cos, row in scored[:k]:
        va = row.get("valid_at") or ""
        ia = row.get("invalid_at") or ""
        print(f"  [{cos:.3f}] {row.get('fact')}  (valid {va} | invalid {ia})")
    return scored


async def main():
    msgs = load_text_messages()
    sessions = to_sessions(msgs)
    logger.warning("message=%d sessions=%d", len(msgs), len(sessions))

    def take_sessions(sessions, msg_limit, min_len=4):
        picked, n = [], 0
        for s in sessions:
            if n >= msg_limit:
                break
            if len(s) < min_len:  # 跳过过短会话, 控制 episode 数量与成本
                continue
            picked.append(s)
            n += len(s)
        return picked

    early = take_sessions(sessions, EARLY_MSGS)
    late = take_sessions(sessions[::-1], LATE_MSGS)
    logger.warning("sample: early=%d late=%d early_msgs=%d late_msgs=%d",
                   len(early), len(late), sum(map(len, early)), sum(map(len, late)))

    # ---- Graphiti 初始化 ----
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("缺少 LLM API Key：请设置环境变量 LLM_API_KEY")
    llm_client = DashScopeRepairClient(
        config=LLMConfig(
            api_key=api_key,
            model=LLM_MODEL,
            small_model=LLM_MODEL,
            base_url=BASE_URL,
            temperature=0,
        ),
        max_tokens=8000,  # DeepSeek 侧按需下调
        structured_output_mode="json_object",
    )
    embedder = FastEmbedLocal(LOCAL_EMBED_MODEL, LOCAL_EMBED_DIM)
    # 检索仅走语义回退(不调用 cross_encoder); 显式传入仅为满足初始化
    reranker = OpenAIRerankerClient(config=LLMConfig(api_key=api_key, model=LLM_MODEL, base_url=BASE_URL))
    DB_DIR.parent.mkdir(parents=True, exist_ok=True)
    driver = KuzuDriver(db=str(DB_DIR))

    graphiti = Graphiti(
        llm_client=llm_client,
        embedder=embedder,
        graph_driver=driver,
        cross_encoder=reranker,
    )
    await graphiti.build_indices_and_constraints()
    await ensure_fts(driver)

    # ---- 摄入 ----
    async def ingest(episodes, prefix):
        lat = []
        np_, ne = [], []
        for i, session in enumerate(episodes):
            body = session_body(session)
            dt = datetime.fromtimestamp(session[0][0], TZ)
            name = f"{prefix}_{i:03d}_{dt.strftime('%Y%m%d')}"
            t0 = time.perf_counter()
            res = await graphiti.add_episode(
                name=name,
                episode_body=body,
                source=EpisodeType.message,
                source_description=f"私聊会话 {dt.strftime('%Y-%m-%d')} A × B",
                reference_time=dt,
            )
            lat.append(time.perf_counter() - t0)
            np_.append(len(res.nodes))
            ne.append(len(res.edges))
            logger.warning("[%s %d] msgs=%d nodes=%d edges=%d %.1fs",
                           prefix, i, len(session), len(res.nodes), len(res.edges), lat[-1])
        return lat, np_, ne

    async def show_search(q, k=5):
        try:
            print(f"\n### Q(graphiti hybrid): {q}")
            edges = await graphiti.search(q, num_results=k)
            for e in edges:
                va = getattr(e, "valid_at", None) or ""
                ia = getattr(e, "invalid_at", None) or ""
                print(f"  - {e.fact}  [valid {va} | invalid {ia}]")
        except Exception as ex:
            logger.warning("graphiti.search 失败(下探语义回退): %s", str(ex)[:160])
            await semantic_search(embedder, driver, q, k=k)

    async def graph_stats():
        out = {}
        for label, q in [
            ("entity_nodes", "MATCH (n:Entity) RETURN count(n) AS c"),
            ("entity_edges", "MATCH (n:RelatesToNode_) RETURN count(n) AS c"),
            ("episodes", "MATCH (n:Episodic) RETURN count(n) AS c"),
        ]:
            r, _, _ = await driver.execute_query(q)
            out[label] = r[0]["c"] if r and r[0] else 0
        return out

    # ---- 阶段 1: 只摄入早期 ----
    print("\n" + "=" * 70)
    print(f"阶段1: 摄入早期 {len(early)} 个会话")
    print("=" * 70)
    t0 = time.perf_counter()
    lat1, np1, ne1 = await ingest(early, "early")
    total_ingest = time.perf_counter() - t0
    await show_search("A 和 B 刚开始认识时聊了什么")
    await show_search("B 对什么感兴趣 有什么偏好")

    # ---- 阶段 2: 追加近期会话, 观察时间化事实 ----
    print("\n" + "=" * 70)
    print(f"阶段2: 追加近期 {len(late)} 个会话")
    print("=" * 70)
    t0 = time.perf_counter()
    lat2, np2, ne2 = await ingest(late, "early")
    total_ingest += time.perf_counter() - t0
    await show_search("A 和 B 最近的安排 计划")
    await show_search("B 现在的生活状态 想法")

    # ---- 图统计 + 实体抽样 ----
    stats = await graph_stats()
    r, _, _ = await driver.execute_query("MATCH (n:Entity) RETURN n.name AS name ORDER BY n.name LIMIT 40")
    names = [row["name"] for row in (r or [])]
    print("\n### 抽取出的实体(前40):")
    print("  " + " | ".join(names))

    return {
        "early_episodes": len(early),
        "late_episodes": len(late),
        "total_msgs": sum(map(len, early)) + sum(map(len, late)),
        "stats": stats,
        "nodes_per_ep(early)": np1,
        "edges_per_ep(early)": ne1,
        "latencies(early)": [round(x, 1) for x in lat1],
        "total_ingest_s": round(total_ingest, 1),
    }


if __name__ == "__main__":
    summary = asyncio.run(main())
    print("\n" + "=" * 70)
    print("PoC summary:", json.dumps(summary, ensure_ascii=False, indent=2))