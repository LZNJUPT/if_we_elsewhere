# ARCHITECTURE

IfWe turns "relationship retrospection + counterfactual replay" into a
local-first, explainable, privacy-controlled product. This document describes
the architecture and the reasoning behind key choices.

## Layers

```
┌────────────────────────────────────────────────────────────────────┐
│ T4 Product    phase15_web (vanilla HTML/CSS/JS, zero build)         │
│               run.py (init/analyze/server/demo)                     │
├────────────────────────────────────────────────────────────────────┤
│ T3 Service    phase15_api (FastAPI, 127.0.0.1 only)                 │
│               /api/state /api/anchors /api/history /api/recall      │
│               /api/lines /api/say /api/day /api/media               │
├────────────────────────────────────────────────────────────────────┤
│ T2 Core       DialEngine (message-driven replay kernel)             │
│               PersonaAgent (persona + emotion + medium + anti-rept) │
│               MemoryRetriever (SQL + optional vector, bi-temporal)  │
│               RelEngine (5-dim relationship: events+echo+regression)│
├────────────────────────────────────────────────────────────────────┤
│ T1 Data       single-file SQLite (WAL)                              │
│               main: messages/sessions/events/facts/relationship_*   │
│               sandbox: sim_* namespaces (replay-only; main is RO)   │
└────────────────────────────────────────────────────────────────────┘
```

## Key decisions

### 1. Two-tier memory

- **Long-term (facts)**: a bi-temporal fact table — `valid_at/invalid_at`
  expresses "when it held", and `as_of` queries guarantee the agent only sees
  what was knowable at that time. Scoring = relevance (IDF-weighted CJK
  bigrams) + confidence + time decay × strength (reinforced on access).
- **Short-term (WorkingMemory)**: recent dialogue within a character budget;
  when exceeded, the oldest half is folded into a summary written back to
  long-term memory. After a restart the buffer is rebuilt from the database.
- **Fusion**: main-DB memories and branch-local `sim_facts` are merged with
  RRF. After an IF branch's fork point, real memories are truncated to that
  point (counterfactual freeze) so the model cannot "peek" at the rewritten
  future.

### 2. Why a homegrown memory instead of Graphiti

We built a full Graphiti PoC (embedded Kuzu graph; see [../poc/README_EN.md](../poc/README_EN.md)).
Temporal knowledge graphs are expressive, but:

- construction is expensive (per-session LLM entity/relation extraction);
- the operational complexity of an embedded graph DB exceeds what a
  single-user local tool needs;
- entity extraction quality on Chinese corpus was mediocre — noisy entities
  polluted context.

A single bi-temporal facts table plus hybrid keyword/vector retrieval covers
most of the value with zero service dependencies, sub-second queries, and a
fully auditable data structure.

### 3. Why 5 dimensions with explanation chains, not a single score

Relationships are multi-dimensional: a fight raises conflict while eroding
closeness and emotional safety, yet honest repair talk may raise trust. A
single score hides that structure. RelEngine maps 12 event types to 5-dim
deltas (severity/importance scaling, monthly tanh compression, echo decay),
and every state change can answer "why".

### 4. Simulation isolation (a red-line design)

Replays write exclusively to `sim_*` tables plus the `ifr_branch` metadata
table; the main database is **read-only** on the replay path. The offline
selftest (`python app/phase15_dial_engine.py selftest`) verifies
"main DB unchanged" automatically.

### 5. Message-driven time

The product has no god-view schedule: each user message is one turn, and time
advances a day every N messages (default 20) or on demand. Events settle daily
(same-type events count once per day, decayed on the second, capped after) to
prevent score farming.

## Repository layout

```
if_we_elsewhere/
├── run.py                  # one-shot entry (init/analyze/server/demo)
├── config.example.yaml     # config template (copy to config.yaml)
├── app/
│   ├── config.py           # config loader (env > yaml > defaults)
│   ├── phase1_ingest.py    # import / sanitize / sessionize / gates (library)
│   ├── phase2_llm.py       # robust structured-output LLM layer
│   ├── phase4_retrieval.py # memory retrieval (bigram+IDF+vector, bi-temporal)
│   ├── phase5_common.py    # sim clock / world state / branch memory fusion
│   ├── phase5_a2_loop.py   # PersonaAgent + session loop + event logging
│   ├── phase5_a3_buffer.py # short-term buffer with summary folding
│   ├── phase5_a5_pcc.py    # persona-consistency calibration (plan/reflect)
│   ├── phase6_engine.py    # 5-dim relationship engine
│   ├── phase15_dial_engine.py # replay kernel (you + digital persona)
│   ├── phase15_api.py      # FastAPI service layer
│   ├── phase15_web/        # zero-build vanilla frontend
│   └── schema*.sql         # all DDL (idempotent)
├── scripts/
│   ├── import_chat.py      # import CLI
│   └── check_privacy.py    # privacy scan gate
├── sample_data/            # purely fictional sample data
├── poc/                    # optional PoCs (graphiti comparison, etc.)
└── docs/
```

## Stack

- **Backend**: Python stdlib + SQLite + FastAPI/uvicorn + pydantic + openai SDK
  (any OpenAI-compatible endpoint)
- **Retrieval**: plain SQL (CJK bigrams + IDF) baseline; optional local ONNX
  (fastembed) vector enhancement
- **Frontend**: vanilla HTML/CSS/JS — no build step, no CDN
- **LLM**: json_object mode + pydantic validation + field-level type repair +
  multi-retry (engineering fallbacks for empty/truncated/drifted outputs)
