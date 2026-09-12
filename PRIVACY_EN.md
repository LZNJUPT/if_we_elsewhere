# Privacy (PRIVACY_EN)

> One sentence: **your data belongs to you.** Everything heavy runs locally; only
> one step — generating a reply — sends a clearly-scoped, sanitized context to the
> LLM API you configure yourself.

## Data flow

```
┌─────────────────────────── your computer (local) ───────────────────────┐
│                                                                          │
│  raw JSONL (read-only, never modified)                                   │
│      │  Phase1: denoise + sanitize (phones/addresses/IDs/cards →          │
│      │        placeholders)                                              │
│      ▼                                                                   │
│  data/ifwe_v1.db  ← messages (content_orig stays local) / sessions /      │
│      │              events / facts / relationship_state / …               │
│      │                                                                   │
│      ├─► analyze: events, memories, relationship state, turning points,  │
│      │        personas  (LLM path sends sanitized text only; the         │
│      │         --skip-llm path is fully local)                           │
│      ▼                                                                   │
│  DialEngine replay: writes only sim_* namespaces; main DB is read-only   │
│      │                                                                   │
│      │  ⚠ the only network egress: reply generation sends this to YOUR   │
│      │    LLM API:                                                       │
│      │    - persona profile (LLM- or hand-written personality summary)   │
│      │    - retrieved memories (derived from sanitized text)             │
│      │    - recent chat buffer (sanitized text)                          │
│      │    - your input and the rewrite scene card                        │
│      ▼                                                                   │
│  your LLM API (DeepSeek / any OpenAI-compatible endpoint / local model)  │
└──────────────────────────────────────────────────────────────────────────┘
```

## Concrete guarantees

| Layer | What we do |
|---|---|
| Raw records | The source file is never modified; `content_orig` lives only in your local DB and never feeds any outbound path |
| Sanitization | Medium-level sanitization runs **before** anything is stored; `content_clean` is the only text source for analysis and LLM calls |
| Simulation isolation | Replays write to `sim_*` tables; the main DB gets zero writes during a replay (verified automatically by the offline selftest) |
| Counterfactual freeze | After a branch's fork point, real memories are time-truncated, so the model cannot see the future you rewrote |
| Keys | Read from environment variables only (default `LLM_API_KEY`), never written to disk or the repo |
| Repo hygiene | `data/` and `config.yaml` are always gitignored; sample data is purely fictional |

## The privacy gate (this project's own bar)

`scripts/check_privacy.py` is a release gate: it scans every text file in the
repo against a red-line word list and exits non-zero on any hit. It exists to
answer one question while turning a private project into a public one — did
anyone's real nicknames, places, date anchors or events leak into code or docs?

```bash
python scripts/check_privacy.py                   # run before commit / release
python scripts/check_privacy.py --extra my_words.txt   # extend the word list
```

Wire it into pre-commit or CI. This project's own releases were gated by it.

## Residual risks you should know about

- Your LLM provider sees whatever sanitized text you send (subject to their
  policy; use a local model to eliminate this entirely).
- `content_orig` keeps original text in your local DB — that is the foundation
  of the "real history replay" feature. Protect your `data/` folder like a
  database dump.
- Sticker replay renders images locally in the UI; nothing is uploaded.

Questions or suggestions about privacy? Open an issue.
