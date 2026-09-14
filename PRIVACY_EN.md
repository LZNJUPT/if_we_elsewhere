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
| Keys | Environment variable first (default `LLM_API_KEY`); keys entered in the in-app Settings panel go to the **Windows Credential Manager** (service `IfWe`, account = `llm.api_key_env`), with a current-user **DPAPI-encrypted** file (`data/.secret_llm_key`) as fallback. `config.yaml`, logs and every API response stay key-free (`GET /api/settings` returns only `sk-***abc`) |
| Web import | Uploaded files land only in the local temp directory `data/tmp_import/`; the upload → preview → commit chain **deletes them immediately when it ends** (including failures); preview returns statistics and sanitized samples only — never raw text or server paths. **Source archives are kept on purpose** in `data/profiles/<id>/sources/` so later imports can rebuild the whole timeline (see below); the UI can remove any of them |
| Media | Stickers/images are stored as **raw, unsanitized** binaries in `data/profiles/<id>/media/`; they never enter analysis or LLM calls, only the display layer |
| Friend isolation | One data directory per friend (`data/profiles/<id>/`: database, persona, sources, media, conversations); switching friends switches the whole data context. Pre-migration backups go to `data_backup_<date>/` (gitignored) |
| Repo hygiene | `data/`, `data_demo/`, `config.yaml` and `data_backup_*/` are always gitignored; sample data is purely fictional |

## Web import wizard data flow (v0.2, extended to multiple sources in v0.3)

The "Import" entry in the left sidebar uses the same local pipeline as the CLI:

1. **Upload**: files are written to a local directory `data/tmp_import/<random-id>`
   (inside `data/`, already gitignored); `.jsonl / .json / .csv / .txt / .md /
   .log / .docx` are accepted (`.pdf` is only detected and told to be re-saved),
   up to 50MB per file and 12 files per batch, one import task at a time (file lock);
2. **Preview**: format detection, statistics, sanitized samples and a trial merge are
   computed locally; the response contains statistics and up to 3 **sanitized** samples
   per side — no raw text, no server paths;
3. **Commit**: the batch is archived under `data/profiles/<id>/sources/` and registered
   in `sources.json`, then the DB is rebuilt from **all registered sources** + quality
   gates run in the same process;
4. **Deletion**: after a failed preview or whenever the commit ends (success or
   failure), the **temp file and lock are deleted immediately**; tasks older
   than 1 hour are reclaimed automatically. Note that **source archives are kept on
   purpose** (see the next section).

Everything happens on your computer; this project has no server and performs
no remote uploads.

## Source archives and media (v0.3 — you should know this)

Two kinds of local data are **deliberately kept** in v0.3, both so that the very
common case of "the same conversation lives in several apps" actually works:

**1. Source archives**: `data/profiles/<id>/sources/<content-hash-16>.<ext>`.
The DB is always rebuilt from *all registered sources*, so the source files must be
archived — otherwise the second import could not rebuild the first one's data.
The registry is `data/profiles/<id>/sources.json` (filename, format, per-source A/B
mapping, counts — no content). The UI ("Import → registered sources") lets you
review and **remove** any single one (removal deletes its archive and rebuilds);
the CLI can skip registration entirely with `--no-archive`. All of it lives under
the friend's directory and is deleted together with that friend.

**2. Media library**: `data/profiles/<id>/media/<content-hash-16><ext>`

> ⚠️ **Media is not sanitized — this is the big difference from chat text.**
> Chat text has phones / IDs / bank cards / plates / emails / addresses replaced
> with placeholders before it is stored. **Images are raw binaries and get no
> processing at all.** A screenshot of an ID card, a courier label, or a chat
> screenshot containing an address will sit unchanged in the directory above.
>
> Guarantees that come with it:
> - media **never participates in analysis and is never sent to the LLM** (only
>   sanitized text is ever sent);
> - media is deduplicated by content hash; deleting a media item only affects
>   display and never touches chat records;
> - confirm before importing that the images contain nothing you would rather not
>   keep on this machine.

## Keys and data directories (v0.3, desktop-era)

**Keys** (in priority order): environment variable → Windows Credential Manager (service
`IfWe`) → `data/.secret_llm_key` (DPAPI ciphertext, decryptable only by the current Windows
user). Clear a key with the in-app "clear key" button or `python run.py key clear` (both
locations are wiped).

**Data layout**: `data/profiles.json` (friend registry) + `data/profiles/<id>/` (one full
set per friend: `ifwe_v1.db`, `persona/`, `sources/` + `sources.json`, `media/`,
`profile.yaml`) + `data/tmp_import/` (import wizard, deleted when the chain ends).
API responses only ever contain relative paths (e.g. `data/profiles/ada`).

**Launcher traces**: `data/.ifwe.lock` (single-instance lock: pid + port + start time,
removed on clean exit, cleaned up on the next launch otherwise). The server binds to
`127.0.0.1` only, and closing the window stops it. A packaged build keeps `data/` next to
the exe and writes nothing to the registry or system directories.

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
