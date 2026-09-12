# IMPORT · Data import guide (spec v1)

## Compliance boundary (read this first)

- This project does **not** parse WeChat databases and offers **no scraping**
  capability, nor tutorials on how to export.
- You may only import chat logs that **you lawfully exported yourself**, and you
  are responsible for their legality.
- Sanitization happens on import: phones / ID numbers / bank cards / plates /
  emails / addresses are replaced with placeholders like `[手机号]` / `[地址]`.
  The sanitized text (`content_clean`) is the only input for later analysis
  and LLM calls.

## Format supported in v1: chatlab JSONL (WeFlow export)

One JSON object per line:

```json
{"_type":"message","platformMessageId":"xxx","timestamp":1751788200,
 "type":0,"content":"rough day today","accountName":"your_handle"}
```

| Field | Meaning |
|---|---|
| `_type` | must be `"message"` (other lines are skipped) |
| `timestamp` | Unix epoch seconds |
| `type` | `0` text / `7` image or sticker / `4` file / `23` call / `24` mini-program / `25` quoted text / `27` name card / `80` recall / `99` transfer |
| `content` | content (sticker: filename; transfer: text with amount) |
| `accountName` | sender handle — mapped to A/B via `--sender-a/--sender-b` |

> Exports from WeFlow (a local WeChat-export tool) match this format.
> Compatibility with other tools (e.g. WeChatMsg) is on the roadmap (v0.2).

## Steps

```bash
# Option A: one shot
python run.py init --source data/raw/chat.jsonl \
    --sender-a "your_handle" --sender-b "their_handle"

# Option B: step by step
python run.py init
python scripts/import_chat.py --source data/raw/chat.jsonl \
    --sender-a "your_handle" --sender-b "their_handle"
```

You can also put the mapping in `config.yaml` and skip the CLI flags:

```yaml
people:
  A: { key: "A", display: "you", match: "your_handle" }
  B: { key: "B", display: "TA", match: "their_handle" }
chat:
  source: "data/raw/chat.jsonl"
```

> Convention: `--sender-a` is **you** (the "you" of the conversation; you type
> the messages yourself during replays), `--sender-b` is the other person (the
> digital persona side). Handles must match `accountName` exactly.

## What happens after import

1. **Denoise**: whitespace/newlines unified;
2. **Sanitize**: privacy patterns → placeholders, counted in `has_privacy`;
3. **Sessionize**: gaps > 30 minutes split sessions (configurable);
4. **Store**: `messages / sessions / daily_stats` in `data/ifwe_v1.db`;
5. **Quality gates**: time ordering, both-party ratio (>25% each), sanitization
   residue rescan, unknown sender/type — any failure exits non-zero.

Reports land in `data/phase1_验收报告.md` and `data/phase1_summary.json`.

## After import

```bash
python run.py analyze
```

The (simplified v0.1) analysis pipeline produces:

| Artifact | Table/file | Notes |
|---|---|---|
| Events | `events` | session-level extraction (LLM path; `--skip-llm` uses keyword heuristics) |
| Memories | `facts` | bi-temporal facts (episodic) for retrieval |
| Relationship state | `relationship_state` | monthly 5-dim estimate (confidence 0.4, not ground truth) |
| Turning points | `turning_points` | silent windows (≥14 days), top events, peak month |
| Personas | `data/persona/persona_v1_{A,B}.json` | L/M/S/U profiles (LLM-generated or hand-written templates) |

## Stickers (optional)

Point `media.emojis_dir` at your exported sticker folder:

```yaml
media:
  emojis_dir: "D:/weflow/Emojis"
```

Filenames must be 32 hex chars + an image extension (as WeFlow exports).
Without it the UI shows placeholders; everything else works.

## FAQ

**Q: Gate says "both-party ratio" failed?**
Most likely the handle mapping is wrong (a mismatch makes one side "unknown X").

**Q: Message count differs from my export tool?**
Non-`message` lines and unparseable lines are skipped; trust the import report
and compare filters.

**Q: Re-import?**
Each import rebuilds the database by default (unless `--no-reset`); your source
file is read-only and never modified.

**Q: Privacy worries?**
See [../PRIVACY_EN.md](../PRIVACY_EN.md): sanitization precedes storage; only
sanitized text and derived conclusions ever leave; raw content stays in your
local database file.
