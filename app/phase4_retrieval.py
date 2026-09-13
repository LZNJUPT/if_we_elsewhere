# -*- coding: utf-8 -*-
"""
IfWe Phase 4 · M4 检索接口（纯 SQL 中文召回）+ M5 遗忘与强度
- 检索: 中文字符 bigram + SQLite INSTR 过滤 -> Python 打分（relevance/importance/recency/strength）
  纯 SQL 先跑（用户已拍板）；FTS5/向量留作后续可选，脚本提供可用性探测
- M5 遗忘: 记忆强度按时间指数衰减（half-life 可配），检索命中即强化（strength 上调 + last_access 更新）
  打分借鉴 Generative Agents: score = (w_rel*relevance + w_imp*importance + w_rec*recency) * strength_eff
用法:
  python app/phase4_retrieval.py --demo            # 展示若干查询的召回
  python app/phase4_retrieval.py --diagnose        # FTS5 / fastembed 可用性探测
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import numpy as np

import config as cfg_mod

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = cfg_mod.db_path()
RESULT_DIR = cfg_mod.data_dir() / "phase4_results"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOK = re.compile(r"[A-Za-z0-9#+._/\\-]{2,}")

# M5 默认参数
HALF_LIFE_DAYS = 180.0      # 记忆强度半衰期（天）
REINFORCE_DELTA = 0.1       # 单次检索强化增量
W_REL, W_IMP, W_REC = 0.7, 0.1, 0.2   # 打分权重（relevance/importance/recency）

# 查询停用词（中文问句中的功能/套话词，不具区分度；过滤后相关性更聚焦）
STOPWORDS = {
    "什么", "怎么", "为什么", "为什", "之间", "特别", "多少", "大概", "大约", "左右",
    "中旬", "哪些", "一个", "一种", "时候", "发生", "事件", "开始", "是否", "最近",
    "今年", "去年", "方面", "情况", "相关", "当时", "曾经", "分别", "具体", "到底",
    "了", "的", "是", "在", "和", "与", "及", "吗", "呢", "吧", "啊", "更", "最",
    "会", "要", "就", "也", "都", "有", "能", "还", "并", "或", "可", "到",
}


def cjk_bigrams(text: str) -> list[str]:
    cjk = "".join(c for c in (text or "") if _CJK.match(c))
    return [cjk[i:i + 2] for i in range(len(cjk) - 1)]


def query_tokens(query: str) -> list[str]:
    """查询拆 token：先剔除停用词短语，再取 CJK bigram + 连续非中文 token"""
    s = query
    for w in sorted(STOPWORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    toks = []
    for b in cjk_bigrams(s):
        if b and b not in STOPWORDS:
            toks.append(b)
    for t in _TOK.findall(query):
        toks.append(t)
    return toks


def _days_between(a: str, b: str) -> int:
    try:
        ta = time.mktime(time.strptime(a[:10], "%Y-%m-%d"))
        tb = time.mktime(time.strptime(b[:10], "%Y-%m-%d"))
        return int((tb - ta) / 86400)
    except Exception:
        return 0


class MemoryRetriever:
    def __init__(self, db_path=None):
        self.conn = sqlite3.connect(db_path or cfg_mod.db_path())   # 动态：随当前好友切换
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self._token_df: dict[str, int] | None = None
        self._n_docs = 0
        self._emb: tuple[dict, object, object] | None = None   # (index, mat, norms)
        self._model = None

    # ---------------- 向量召回（M4 增强，用户拍板启用） ----------------
    def _load_embeddings(self):
        """从 fact_embedding 表加载向量矩阵（惰性，一次加载缓存）"""
        if self._emb is not None:
            return self._emb
        import numpy as np
        rows = self.conn.execute("SELECT fact_id, vec FROM fact_embedding").fetchall()
        if not rows:
            return None
        ids = [r[0] for r in rows]
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        norms = np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
        index = {fid: i for i, fid in enumerate(ids)}
        self._emb = (index, mat, norms)
        return self._emb

    def _query_vec(self, query: str):
        if self._model is None:
            from fastembed import TextEmbedding
            cache = cfg_mod.cache_dir() / "fastembed"
            cache.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5",
                                        cache_dir=str(cache))
        v = list(self._model.embed([query]))[0]
        import numpy as np
        return np.asarray(v, dtype=np.float32)

    def _cosine(self, fact_id: str, qv, qnorm) -> float | None:
        emb = self._emb
        if emb is None:
            return None
        index, mat, norms = emb
        i = index.get(fact_id)
        if i is None:
            return None
        import numpy as np
        return float(float(np.dot(mat[i], qv)) / float(norms[i, 0] * qnorm))

    # ---------------- IDF（token 稀有度，改善中文检索排名；仍为纯 SQL+Python） ----------------
    def _build_token_df(self) -> dict[str, int]:
        if self._token_df is not None:
            return self._token_df
        df: dict[str, int] = {}
        n = 0
        for (kw,) in self.conn.execute("SELECT keywords FROM facts").fetchall():
            n += 1
            for t in set((kw or "").split()):
                df[t] = df.get(t, 0) + 1
        self._token_df = df
        self._n_docs = n
        return df

    def _idf(self, token: str) -> float:
        df = self._build_token_df()
        return math.log((self._n_docs + 1) / (df.get(token, 0) + 1)) + 1.0

    # ---------------- M5 遗忘/强度 ----------------
    @staticmethod
    def decay_strength(strength: float, last_access: Optional[str], now: str,
                       halflife: float = HALF_LIFE_DAYS) -> float:
        """时间衰减：从未访问则视为刚创建(不衰减)；否则按半衰期指数衰减"""
        if not last_access:
            return strength
        d = _days_between(last_access, now)
        if d <= 0:
            return strength
        return strength * (0.5 ** (d / halflife))

    def reinforce(self, fact_ids: list[str], now: str | None = None) -> int:
        """检索命中即强化（M5）：strength 上调 + last_access 更新 + 访问计数 +1"""
        now = now or time.strftime("%Y-%m-%d %H:%M:%S")
        if not fact_ids:
            return 0
        cur = dict(self.conn.execute(
            "SELECT fact_id, strength FROM facts WHERE fact_id IN (%s)"
            % ",".join("?" * len(fact_ids)), fact_ids).fetchall())
        n = 0
        for fid in fact_ids:
            if fid not in cur:
                continue
            s = min(1.0, (cur[fid] or 0.0) + REINFORCE_DELTA)
            self.conn.execute(
                "UPDATE facts SET strength=?, last_access=?, access_count=access_count+1 WHERE fact_id=?",
                (round(s, 4), now, fid))
            n += 1
        self.conn.commit()
        return n

    # ---------------- 检索 ----------------
    def search(self, query: str, top_k: int = 5, as_of: str | None = None,
               apply_forgetting: bool = True, mode: str = "hybrid",
               w_rel: float = W_REL, w_imp: float = W_IMP, w_rec: float = W_REC) -> list[dict]:
        """统一检索接口。mode: sql(纯关键词) / vector(纯向量) / hybrid(默认=RRF 融合 SQL+向量)
        as_of=评估/查询时间点；apply_forgetting=False 时强度视为 1（用于评测对照）"""
        as_of = as_of or time.strftime("%Y-%m-%d")
        toks = query_tokens(query)
        rows = self.conn.execute(
            """SELECT fact_id, memory_type, subject, relation, object, conclusion_text,
                      valid_at, invalid_at, source, source_id, confidence, strength, last_access, keywords
               FROM facts WHERE valid_at <= ? AND (invalid_at IS NULL OR invalid_at > ?)""",
            (as_of, as_of)).fetchall()
        cols = ["fact_id", "memory_type", "subject", "relation", "object", "conclusion_text",
                "valid_at", "invalid_at", "source", "source_id", "confidence", "strength",
                "last_access", "keywords"]
        # 查询 token 的 IDF 权重（稀有 token 更重要，避免"两人/年"等高频词稀释）
        toks_set = list(dict.fromkeys(toks))
        tok_idf = {t: self._idf(t) for t in toks_set}
        denom = sum(tok_idf.values())
        # 向量召回（惰性加载）
        emb = self._load_embeddings() if mode in ("vector", "hybrid") else None
        qv = self._query_vec(query) if (emb is not None and mode in ("vector", "hybrid")) else None
        qnorm = float(np.linalg.norm(qv)) if qv is not None else 1.0

        # 单趟计算每行的 sql 基础分 + 向量相似度
        sql_list, vec_list = [], []
        for r in rows:
            d = dict(zip(cols, r))
            kw = (d["keywords"] or "")
            matched = [t for t in toks_set if t in kw]
            relevance = (sum(tok_idf[t] for t in matched) / denom) if (denom and matched) else 0.0
            importance = float(d["confidence"] or 0.5)
            # recency: 事件/关系状态=时间衰减；语义/程序性(persona/实体)=时不变知识，取中性 0.5
            if d["memory_type"] in ("episodic", "state"):
                vd = _days_between((d["valid_at"] or as_of), as_of)
                recency = math.exp(-max(0, vd) / 365.0) if vd >= 0 else 0.0
            else:
                recency = 0.5
            strength = (self.decay_strength(float(d["strength"] or 1.0), d["last_access"], as_of)
                        if apply_forgetting else 1.0)
            base = w_rel * relevance + w_imp * importance + w_rec * recency
            rec = {**d, "relevance": round(relevance, 3), "importance": round(importance, 3),
                   "recency": round(recency, 3), "strength_eff": round(strength, 3)}
            sql_list.append((rec, base * strength))
            if qv is not None and emb is not None:
                sim = self._cosine(d["fact_id"], qv, qnorm)
                if sim is not None:
                    vec_list.append((rec, sim, strength))
        # 排名
        if mode == "vector" and vec_list:
            vec_list.sort(key=lambda x: x[1], reverse=True)
            for rec, sim, strength in vec_list:
                rec["vec_sim"] = round(sim, 3)
                rec["score"] = round(sim * strength, 4)
            return [v[0] for v in vec_list[:top_k]]
        if mode == "sql" or not vec_list:
            sql_list.sort(key=lambda x: x[1], reverse=True)
            for rec, s in sql_list:
                rec["vec_sim"] = None
                rec["score"] = round(s, 4)
            return [v[0] for v in sql_list[:top_k]]
        # hybrid = RRF（倒数排名融合 SQL + 向量，k=60，各取 top50）
        RRF_K, RRF_N = 60, 50
        sql_top = sorted(sql_list, key=lambda x: x[1], reverse=True)[:RRF_N]
        vec_top = sorted(vec_list, key=lambda x: x[1], reverse=True)[:RRF_N]
        fusion: dict[str, tuple[dict, float]] = {}
        for i, (rec, _) in enumerate(sql_top):
            f = fusion.get(rec["fact_id"], [rec, 0.0])
            f[1] += 1.0 / (RRF_K + i + 1)
            fusion[rec["fact_id"]] = f
        for i, (rec, _, _) in enumerate(vec_top):
            f = fusion.get(rec["fact_id"], [rec, 0.0])
            f[1] += 1.0 / (RRF_K + i + 1)
            fusion[rec["fact_id"]] = f
        ranked = sorted(fusion.values(), key=lambda x: x[1], reverse=True)
        for rec, fscore in ranked:
            vs = next((sim for r2, sim, _ in vec_list if r2["fact_id"] == rec["fact_id"]), None)
            rec["vec_sim"] = round(vs, 3) if vs is not None else None
            rec["score"] = round(fscore, 4)
        return [rec for rec, _ in ranked[:top_k]]

    def search_structured(self, query: str, top_k: int = 5, as_of: str | None = None,
                          apply_forgetting: bool = True, mode: str = "hybrid",
                          memory_type: str | None = None,
                          source: str | None = None) -> list[dict]:
        """带类型/来源过滤的检索（供评测按需取上下文）"""
        res = self.search(query, top_k=top_k * 3, as_of=as_of,
                          apply_forgetting=apply_forgetting, mode=mode)
        if memory_type:
            res = [r for r in res if r["memory_type"] == memory_type]
        if source:
            res = [r for r in res if r["source"] == source]
        return res[:top_k]


# ---------------- 可用性探测 ----------------
# （对齐召回质量测试属研究指标，v0.1 未收录；见 README 路线图）
def diagnose() -> dict:
    diag = {"run_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        diag["fts5_available"] = True
    except Exception as e:
        diag["fts5_available"] = False
        diag["fts5_error"] = str(e)[:80]
    try:
        from fastembed import TextEmbedding  # noqa
        diag["fastembed_available"] = True
    except Exception as e:
        diag["fastembed_available"] = False
        diag["fastembed_error"] = str(e)[:80]
    return diag


def main():
    ap = argparse.ArgumentParser(description="IfWe Phase 4 M4/M5 检索与遗忘")
    ap.add_argument("--demo", action="store_true", help="展示若干查询召回")
    ap.add_argument("--diagnose", action="store_true", help="FTS5/fastembed 可用性探测")
    ap.add_argument("--query", type=str, default="", help="单条查询")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--mode", type=str, default="hybrid", choices=["sql", "vector", "hybrid"])
    args = ap.parse_args()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    retriever = MemoryRetriever()

    if args.diagnose:
        d = diagnose()
        d["fact_embeddings"] = retriever.conn.execute(
            "SELECT COUNT(*) FROM fact_embedding").fetchone()[0]
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return

    queries = [args.query] if args.query else [
        "最近一起做了什么", "情绪低落需要安慰", "讨论过哪些未来计划"]
    for q in queries:
        if not q:
            continue
        print(f"\n== 查询: {q} (mode={args.mode}) ==")
        res = retriever.search(q, top_k=args.k, mode=args.mode)
        for r in res:
            print(f"  [{r['score']:.3f}] ({r['memory_type']}/{r['subject']}) "
                  f"valid={r['valid_at']}~{r['invalid_at'] or '今'} src={r['source']}:{r['source_id']}")
            print(f"      {r['conclusion_text'][:80]}")
        retriever.reinforce([r["fact_id"] for r in res])  # 检索即强化（M5）


if __name__ == "__main__":
    main()
