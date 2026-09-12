# -*- coding: utf-8 -*-
"""仅本地检索演示: 对已落盘的 Kuzu 图做语义相似检索(不调用任何 LLM API)"""
import asyncio
import os
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import kuzu
import numpy as np

from graphiti_core.embedder.client import EmbedderConfig

# 仅本地演示：对已落盘的 Kuzu 图做语义检索；图库位置在 poc/data/kuzu_chat（gitignore 内）
DB = str(Path(__file__).resolve().parent / "data" / "kuzu_chat")


class LocalEmbed:
    def __init__(self):
        from fastembed import TextEmbedding

        self._model = TextEmbedding("BAAI/bge-small-zh-v1.5")
        self.config = EmbedderConfig(embedding_dim=512)

    async def embed_query(self, text):
        return [float(x) for x in next(self._model.embed([text]))]


async def main():
    conn = kuzu.AsyncConnection(kuzu.Database(DB))
    embed = LocalEmbed()

    # 统计
    for label, q in [
        ("episodes", "MATCH (n:Episodic) RETURN count(n) AS c"),
        ("entities", "MATCH (n:Entity) RETURN count(n) AS c"),
        ("facts", "MATCH (n:RelatesToNode_) RETURN count(n) AS c"),
        ("facts_invalid", "MATCH (n:RelatesToNode_) WHERE n.invalid_at IS NOT NULL RETURN count(n) AS c"),
    ]:
        r = await conn.execute(q)
        rows = list(r.rows_as_dict())
        print(f"{label}: {rows[0]['c'] if rows else 0}")

    r = await conn.execute(
        "MATCH (n:RelatesToNode_) RETURN n.fact AS fact, n.fact_embedding AS emb, "
        "n.valid_at AS valid_at, n.invalid_at AS invalid_at"
    )
    facts = list(r.rows_as_dict())

    queries = [
        "B 喜欢吃什么 有什么口味偏好",
        "A 最近有什么计划 打算做什么",
        "他们有讨论过什么引起争执或分歧的事情",
    ]
    for q_text in queries:
        q_emb = np.array(await embed.embed_query(q_text), dtype=float)
        scored = []
        for row in facts:
            emb = row.get("emb")
            if not emb:
                continue
            e = np.array(emb, dtype=float)
            denom = (np.linalg.norm(q_emb) * np.linalg.norm(e)) or 1.0
            scored.append((float(np.dot(q_emb, e) / denom), row))
        scored.sort(key=lambda x: x[0], reverse=True)
        print(f"\n### Q: {q_text}")
        for cos, row in scored[:6]:
            va = row.get("valid_at") or ""
            ia = row.get("invalid_at") or ""
            print(f"  [{cos:.3f}] {row['fact']}  (valid={va} invalid={ia})")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())