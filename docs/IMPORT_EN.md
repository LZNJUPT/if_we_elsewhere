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

## Where data comes from (v0.2)

IfWe **only consumes chat log files you lawfully exported yourself** and never
parses any messaging app's database. Supported export sources:

| Format ID | Source tool (official repo/docs) | Export artifact | Notes |
|---|---|---|---|
| `chatlab` | [WeFlow](https://github.com/hicccc77/WeFlow) | JSONL | IfWe's native format, best supported |
| `wecomsg` | [WeChatMsg / MemoTrace](https://github.com/LC044/WeChatMsg) | CSV | messages without a canonical type (voice, video, system notices…) are skipped and counted |
| `telegram` | [Telegram Desktop](https://telegram.org/blog/export-and-more) official export | Machine-readable JSON (`result.json`) | official feature, no compliance disputes |

> Links are source pointers only — **not tutorials**. Perform exports inside
> each tool's official documentation. Group chats are not supported: import
> one-on-one conversations only.

## Format 1: chatlab JSONL (WeFlow export, native)

One JSON object per line:

```json
{"_type":"message","platformMessageId":"xxx","timestamp":1751788200,
 "type":0,"content":"rough day today","accountName":"your_handle"}
```

| Field | Meaning |
|---|---|
| `_type` | must be `"message"` (other lines are skipped) |
| `timestamp` | Unix epoch seconds (v0.2 also accepts milliseconds / ISO8601 / aliases `ts`/`time`) |
| `type` | `0` text / `7` image or sticker / `4` file / `23` call / `24` mini-program / `25` quoted text / `27` name card / `80` recall / `99` transfer |
| `content` | content (sticker: filename; transfer: text with amount; aliases `text`/`message`) |
| `accountName` | sender handle — mapped to A/B via `--sender-a/--sender-b` (aliases `sender`/`talker`/`nick`) |

## Format 2: WeChatMsg (MemoTrace) CSV export

Export "chat history → CSV" (current stable `id,MsgSvrID,type_name,is_sender,
talker,room_name,msg,src,CreateTime` columns; the legacy combined `content`
column is also supported). Text/image/sticker/file/call/quote/name card/
transfer/recall are imported; voice, video, system notices and other messages
without a canonical type are **skipped and counted** (no guessing); group-chat
rows are skipped. Handles are usually wxids — confirm via the doctor report or
the web preview candidate list before mapping.

## Format 3: Telegram Desktop official JSON export

Telegram Desktop → Settings → Advanced → Export chat history →
Machine-readable JSON (`result.json`). Text (including mixed-array entity
concatenation), photos, stickers and files are imported; voice messages, edit
events and service rows are skipped and counted; rows beyond two senders are
treated as group chatter and skipped. Timestamps with an offset are parsed with
their own offset; naive timestamps are read as UTC+8 (matching the project's
v1 convention).

## Import doctor (read-only preflight)

Not sure whether a file can be imported? Run the read-only check:

```bash
python run.py doctor --source your_export.csv
```

It reports: detected format, valid message count, time span, candidate handles
with share, message-type distribution, estimated privacy-pattern hits, and a
verdict (importable / what's missing / suggested flags). Unrecognized files
produce a reason list instead of a crash.

## Steps

```bash
# Option A: one shot (--format defaults to auto-detection)
python run.py init --source data/raw/chat.jsonl \
    --sender-a "your_handle" --sender-b "their_handle"

# Option B: step by step
python run.py init
python scripts/import_chat.py --source data/raw/chat.csv \
    --format wecomsg \
    --sender-a "your_handle" --sender-b "their_handle"
```

`--format` accepts `auto` (default) / `chatlab` / `wecomsg` / `telegram`.

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
> digital persona side). Handles must match the sender field exactly
> (WeChatMsg usually a wxid; Telegram a from_id).

## Web import wizard

Prefer not to use the CLI? Start the local UI (`python run.py server`) and
open "**Import**" in the left sidebar:

1. **Upload**: drag & drop or pick a `.jsonl / .json / .csv` file (≤50MB);
2. **Preview**: format, time span, type distribution, privacy estimate +
   **A/B dropdowns** (candidate handles with counts and sanitized samples);
3. **Result**: quality-gate summary (ordering / both-party ratio / residue /
   unknown senders).

Uploaded files live in a local temp directory (`data/tmp_import/`) and are
deleted as soon as the import chain ends (including failures); nothing ever
leaves your machine. See [../PRIVACY_EN.md](../PRIVACY_EN.md).

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
  emojis_dir: "path/to/weflow_emojis"
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
