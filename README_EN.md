# IfWe · If We Were Us

<div align="center">

**Turn the words you never got to say into a conversation you can finally run.**

Start from your own chat history, go back to any moment you wish had gone differently,
rewrite one sentence — and let a digital persona grounded in your real memories
and personalities answer "what if" for you.

[中文](README.md) · Quick Start · [Import Guide](docs/IMPORT_EN.md) · [Architecture](docs/ARCHITECTURE_EN.md) · [Privacy](PRIVACY_EN.md) · [Disclaimer](DISCLAIMER_EN.md)

</div>

---

<!-- TODO(before release): put a 30-second demo GIF here (record it with the sample data ONLY;
     using real chat logs is strictly forbidden). Storyboard: docs/demo/README.md -->
![demo](docs/demo/demo.gif)

## What is this

IfWe is a **local-first** relationship timeline tool:

1. **Import** your chat history with someone important (a JSONL file you exported yourself);
2. The system sanitizes, extracts events, builds memories and estimates relationship
   state — all locally;
3. Pick a "fork point" on the timeline — one sentence you did or didn't say that day —
   and **rewrite it**;
4. Keep talking with a digital persona grounded in real memories and personality,
   and see where the relationship might have gone.

> It is not a chat-log manager, and not an "AI girlfriend". It cares about one
> concrete question: **if you had phrased it differently, what then?**

## Highlights

| Feature | Description |
|---|---|
| 🔒 Local-first | Chats, analysis and the database all live in local SQLite; only reply generation sends sanitized context to *your own* LLM API |
| 🧹 Sanitize on import | Phone numbers / addresses / IDs / bank cards are replaced with placeholders at import time; sanitized text is the only input for later stages |
| 🕰 Timeline forks | Monthly message volume, 5-dimension relationship state, turning points (gaps / events / peaks) at a glance |
| 🔀 IF-branch rewrite | Jump to any day + rewrite one sentence; the persona treats the rewrite as fact and carries on |
| 🧠 Two-tier memory | Long-term memory (bi-temporal facts + optional local vector search) + short-term buffer with auto summarization; after a fork, the real future is "frozen" out of context |
| 📊 5-dim relationship | Closeness / conflict / trust / emotional safety / communication quality — an event-driven model with a per-change explanation chain |
| 🛡 Privacy gate | Built-in `check_privacy.py`: scans the repo for privacy residue, exits non-zero on any hit (CI-ready) |
| 🖼 Real stickers | The partner's actual stickers are replayed weighted by usage frequency (point `media.emojis_dir` at your export folder) |

## Quick start in 3 steps

```bash
# 0) Install (Python >= 3.10)
pip install -r requirements.txt

# 1) Try the full UI with fictional sample data — no real data involved
python run.py demo

# 2) When your own data is ready: create config → import → analyze
python run.py init
python run.py init --source your_export.jsonl --sender-a "your_handle" --sender-b "their_handle"
python run.py analyze            # offline path: python run.py analyze --skip-llm

# 3) Launch the local UI (binds to 127.0.0.1 only)
python run.py server             # → http://127.0.0.1:8015
```

See [docs/QUICKSTART_EN.md](docs/QUICKSTART_EN.md) and [docs/IMPORT_EN.md](docs/IMPORT_EN.md)
for details, data formats and FAQ.

## Privacy by design (the core differentiator)

- **Nothing leaves your machine** except the explicit LLM calls you configure; `data/` is
  always gitignored, and personal content never mixes with the codebase.
- **Sanitization boundary**: only `content_clean` (denoised + sanitized) and derived
  conclusions are ever sent to the LLM — never raw logs.
- **Simulation isolation**: every "what-if" conversation lives in a `sim_*` namespace;
  the main database is read-only to the simulator.
- **Self-audit**: `python scripts/check_privacy.py` scans the whole repo against a
  red-line word list and exits non-zero on any hit.
- See [PRIVACY_EN.md](PRIVACY_EN.md).

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit: display names, chat source path,
LLM provider/model, sticker folder, simulation parameters. **API keys live in
environment variables only** (`LLM_API_KEY` by default); any OpenAI-compatible
endpoint works (DeepSeek / GLM / local inference).

## Read before use

- [DISCLAIMER_EN.md](DISCLAIMER_EN.md) — not therapy, not prediction, and generated
  content never represents the real person's will; if you are in crisis, see the
  helplines listed there.
- [PRIVACY_EN.md](PRIVACY_EN.md) — data flow, sanitization boundary, LLM scope.
- Using this tool to track, harass or monitor real people is **strictly forbidden**;
  you are responsible for the legality of imported data.

## Roadmap

- [ ] v0.2 more import formats (WeChatMsg etc.)
- [ ] v0.3 research-metric scripts (sensitivity / scoring / evaluation — out of v0.1 scope)
- [ ] Web form editor for persona files
- [ ] Relationship-state visualization panel (productized research module)
- [ ] Full analysis pipeline (v0.1 ships a simplified one)

## License

[MIT](LICENSE) + additional terms (no tracking / harassment / surveillance of real people).

---

<div align="center">

"We can't go back. But we can practice the words."

</div>
