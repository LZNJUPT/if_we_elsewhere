# IMPORT · Data import guide (spec v2)

## Compliance boundary (read this first)

- This project does **not** parse WeChat databases and offers **no scraping**
  capability, nor tutorials on how to export.
- You may only import chat logs that **you lawfully exported yourself**, and you
  are responsible for their legality.
- Sanitization happens on import: phones / ID numbers / bank cards / plates /
  emails / addresses are replaced with placeholders like `[手机号]` / `[地址]`.
  The sanitized text (`content_clean`) is the only input for later analysis
  and LLM calls.
- **Media (stickers / images) is the exception: it is NOT sanitized.** See the
  "Media import" section below for the hard boundary.

## Where data comes from (v0.3)

IfWe **only consumes chat log files you lawfully exported yourself** and never
parses any messaging app's database. Supported export sources:

| Format ID | Source tool (official repo/docs) | Export artifact | Notes |
|---|---|---|---|
| `chatlab` | [WeFlow](https://github.com/hicccc77/WeFlow) | JSONL | IfWe's native format, best supported |
| `wecomsg` | [WeChatMsg / MemoTrace](https://github.com/LC044/WeChatMsg) | CSV | messages without a canonical type (voice, video, system notices…) are skipped and counted |
| `telegram` | [Telegram Desktop](https://telegram.org/blog/export-and-more) official export | Machine-readable JSON (`result.json`) | official feature, no compliance disputes |
| `plaintext` | any tool / hand-curated | `.txt` / `.md` / `.log` | one line per message (`2024-09-11 09:30:00 Alex: text`); parsed with in-line heuristics |
| `docx` | Word save-as | `.docx` | paragraphs, or a "time / sender / text" table; zero third-party dependencies |

> Links are source pointers only — **not tutorials**. Perform exports inside
> each tool's official documentation. Group chats are not supported: import
> one-on-one conversations only.
>
> **PDF is not parsed**: the doctor detects it and tells you to save as txt or
> docx instead. PDF text is a set of absolutely-positioned fragments, so
> "who said what, when" cannot be reconstructed reliably.

## Multi-source merge (v0.3 core)

Real life is rarely "one export, one file": the same conversation may have been
exported on WeChat once, on Telegram once, and later tidied into a txt. Import is
therefore **cumulative, mergeable and individually removable**:

- **1-12 files per batch**, formats may be mixed (a WeChat CSV next to a
  Telegram JSON).
- **Map A/B per source.** Different apps use different handles (WeChat wxid,
  Telegram `from_id`, a nickname inside a txt), so each source gets its own pick
  of "you / them". A single global pair of names would fail on half of them.
- **Merged by timestamp.** Every source is normalized into one canonical stream,
  globally sorted by timestamp, and sessions are recomputed — so two records that
  meet on the timeline naturally become one continuous timeline.
- **Cross-source dedup.** The same sentence inside the overlapping window is kept
  once (key = timestamp + sender + content hash). Gate `G8_跨源重复` reports how
  many were removed and per source.
- **Sources are archived, and the DB is always rebuilt from *all* registered
  sources.** Import WeChat this week and Telegram next week: the second import
  will **not** drop the first one's data.

### Where archives live

Per-friend data directory (everything under `data/`, which is gitignored):

```
data/profiles/<id>/sources/<content-hash-16>.<ext>   source archive (dedup by hash)
data/profiles/<id>/sources.json                      registry (name / format / per-source A/B / counts)
```

"Import → registered sources" in the UI lists every source and lets you remove
one individually (removal deletes its archive and rebuilds from the rest).
`--no-archive` on the CLI skips registration entirely.

### If you swap A/B by mistake

Swapping A/B makes cross-source dedup **silently stop working** (different sender
means a different dedup key, so the same sentence becomes two rows). The importer
therefore self-checks and warns explicitly:

> N groups of messages share the same "timestamp + content" but sit on opposite
> sides — one source's A/B is probably swapped.

You will see this in the web merge preview and in the CLI output.

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

## Format 4: plain text `.txt` / `.md` / `.log` (`plaintext`)

These are *unstructured*: time and sender can only be recognized heuristically, so
three rules apply:

1. **Only anchored, absolute dates are accepted** (`2024-09-11 09:30:00`,
   `2024/9/11 9:30`, `2024年9月11日 9:30`). Relative times like "yesterday" are
   **never guessed** — those lines are simply not imported.
2. **A line without a timestamp is treated as a continuation of the previous
   message** (chat exports wrap text constantly). Lines that follow nothing are
   skipped and counted. The doctor reports `continuation lines merged / lines
   skipped`.
3. **Media lines with no canonical type are skipped and counted** (`[语音]`,
   `[视频]`…), while `[图片]` / `[表情包]` / `[文件]` / `[转账]` map to real types.

Sender notations accepted after the timestamp: `Alex: text` (half/full-width
colon), `**Alex**: text`, `Alex】 text`, `Alex⇥text` (tab-separated), and
sender-before-timestamp (`Alex 2024-09-11 09:30:00 text`).

Encoding is probed in the order BOM → UTF-8 → GB18030, so Chinese exports in the
GBK family read directly.

> Because this is heuristic, **verify the samples shown in the import preview**.
> A low count is usually a layout mismatch, and the doctor tells you which of the
> two it is ("format not recognized" vs "recognized but no messages parsed").

## Format 5: Word `.docx` (`docx`)

- Paragraphs (`w:p`) become lines; tables (`w:tbl`) become one line per row with
  cells joined by TAB.
- Tabs/line breaks inside a paragraph are preserved; images
  (`w:drawing` / `w:pict`) are only counted, never imported (images belong to the
  media channel).
- Parsing uses only the standard library (`zipfile` + `xml.etree`) — **no
  python-docx**.
- Legacy binary `.doc` is not supported; save as `.docx`.

## Import doctor (read-only preflight)

Not sure whether a file can be imported? Run the read-only check:

```bash
python run.py doctor --source your_export.csv
```

It reports: detected format, valid message count, time span, candidate handles
with share, message-type distribution, estimated privacy-pattern hits, the line
parse stats for plain-text formats, and a verdict (importable / what's missing /
suggested flags). Unrecognized files produce a reason list instead of a crash.

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

`--format` accepts `auto` (default) / `chatlab` / `wecomsg` / `telegram` /
`plaintext` / `docx`.

### Several sources at once

`--source`, `--sender-a` and `--sender-b` are repeatable, so each source gets its
own handle mapping:

```bash
python scripts/import_chat.py \
    --source wechat.csv --sender-a wxid_me --sender-b wxid_ta \
    --source tg.json    --sender-a user_me --sender-b user_ta

# later: add one more source — it is merged with everything already registered
python scripts/import_chat.py --source notes.txt --sender-a Alex --sender-b Sam
```

Two switches: `--no-reset` (append the given sources into the existing DB instead
of rebuilding — legacy behaviour, no registration) and `--no-archive` (do not
register the sources).

You can also put the mapping in `config.yaml` and skip the CLI flags. `match`
accepts a list, which covers several sources at once:

```yaml
people:
  A: { key: "A", display: "you", match: ["wxid_me", "user_me", "Alex"] }
  B: { key: "B", display: "TA", match: ["wxid_ta", "user_ta", "Sam"] }
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

1. **Upload**: drag & drop or pick files — **multiple at once**, formats may be
   mixed (`.jsonl / .json / .csv / .txt / .md / .log / .docx`, ≤50MB each,
   ≤12 per batch);
2. **Per-source confirmation + merge preview**: pick "you / them" for every source
   separately; the panel below shows the merged message count, dedup count,
   merged timeline and source overlap, plus the A/B-swap warning. A source that
   cannot be recognized can be removed with "remove this one" while the rest are
   imported normally;
3. **Result**: quality-gate summary (ordering / both-party ratio / residue /
   unknown senders / cross-source duplicates).

Uploaded files live in a local temp directory (`data/tmp_import/`) and are
deleted as soon as the import chain ends (including failures). **Source archives**
are deliberately kept in `data/profiles/<id>/sources/` and registered in
`sources.json` — you can list and remove them from the same panel. Nothing ever
leaves your machine. See [../PRIVACY_EN.md](../PRIVACY_EN.md).

## Media import (stickers / images)

Media is a **separate channel** from chat logs: a chat record only holds a
filename, while the actual image often lives in a different export or in a folder
you collected yourself. So media is imported on its own and then linked to
messages by **exact filename match** (no match means no link — nothing is guessed).

- Import via "Import → open media library": **drag & drop images**
  (`.gif / .png / .jpg / .jpeg / .webp / .bmp`, ≤20MB each), or paste a **local
  directory path** to import a whole folder (typically WeFlow's `Emojis` folder).
- Kind can be "sticker" / "image", or "auto" (`gif`/`webp` count as stickers).
- Stored as `data/profiles/<id>/media/<content-hash-16><ext>`, indexed in the
  `media` table, linked through `message_media`. Deduplicated by content hash.
- The media index is **independent of chat logs**: re-importing chat records or
  removing a source never touches the media library.

### Hard boundary: media is not sanitized and never reaches the LLM

Chat text is sanitized (a phone number becomes `[手机号]`), but **images are raw
binaries and get no processing at all**. A screenshot of an ID card, a courier
label, or a chat screenshot containing an address will sit in
`data/profiles/<id>/media/` exactly as imported.

Therefore: media **never participates in analysis and is never sent to the LLM**
(it is only used to show the images the other person actually used), and you
should confirm before importing that the images contain nothing you would not
want stored locally. Deleting a friend deletes the media library with it.

## What happens after import

1. **Denoise**: whitespace/newlines unified;
2. **Sanitize**: privacy patterns → placeholders, counted in `has_privacy`;
3. **Sessionize**: gaps > 30 minutes split sessions (configurable);
4. **Store**: `messages / sessions / daily_stats` plus the source registry
   `import_sources` in `data/ifwe_v1.db`;
5. **Quality gates**:

| Gate | Check | Blocking |
|---|---|---|
| `G2_时间序列有序` | timestamps non-decreasing after the merge sort | yes |
| `G3_双人占比` | each side > 25% (a wrong mapping shows up here) | yes |
| `G4_长断档告警` | gaps longer than 30 days | warning |
| `G5_脱敏残留` | full rescan of `content_clean` for privacy patterns | yes |
| `G7_未知发送者/类型` | senders not mapped to A/B, unknown types | yes |
| `G8_跨源重复` | dedup count, per-source split, mapping conflicts | warning |

Any blocking failure exits non-zero. Reports land in `data/phase1_验收报告.md`
and `data/phase1_summary.json` (including per-source counts and mappings).

## Data spec v2 (changes from v1)

| Change | Content |
|---|---|
| `messages` new columns | `source_id` (registered source), `dedup_key` (cross-source dedup key) |
| New tables | `import_sources` (source registry), `media` (media index), `message_media` (links) |
| `meta.spec_version` | `"1"` → `"2"` |
| Primary key fix | without a platform message id the fallback id changed from "timestamp-x" to "timestamp + index within source" — messages in the same second no longer overwrite each other |

Old (v1) databases are upgraded idempotently on first access
(`phase1_ingest.ensure_v2_schema()` adds the columns and indexes); no manual
migration is needed.

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
