# QUICKSTART · Up and running in 5 minutes

## 0. Requirements

- Python ≥ 3.10 (3.11/3.12 recommended)
- Windows / macOS / Linux (developed and verified on Windows)

## 1. Install

```bash
git clone https://github.com/LZNJUPT/if_we_elsewhere.git
cd if_we_elsewhere
pip install -r requirements.txt
```

Optional extras (auto-degrade if absent):

```bash
pip install fastembed      # local vector retrieval for hybrid memory search
```

## 2. Try it with sample data (no real data, no API key needed)

```bash
python run.py demo
```

What happens:

1. A **fictional** sample conversation (79 messages, unrelated to any real person)
   is imported into an isolated demo database in `data_demo/`;
2. The offline analysis runs (heuristic events + relationship estimate +
   turning points);
3. Sample personas are installed and the UI starts at `http://127.0.0.1:8015`.

How to play:

- Left panel "＋ 新的一条线" → "接着往下聊": continue chatting with the persona
  starting the day after the sample log ends (sending messages needs an API key);
- "回到过去某天": click a month on the timeline → write what you would do
  differently → open an IF branch;
- "推进一天": advance time and watch the 5-dim relationship state move.

## 2.1 The full flow in the UI (no command line)

On first launch (no `config.yaml`, no messages yet) an **onboarding card** offers three
choices: **try sample data**, **import my logs**, or **configure the LLM**.

Day to day:

- **Analyze** (top bar): tick "离线分析" for the offline path (heuristic events + persona
  templates, zero tokens); leave it unticked for the LLM path (per-session event
  extraction + generated persona). A stage-by-stage progress panel shows the current
  message and elapsed time, and the run can be cancelled. While analyzing, the
  "import / analyze / advance day" buttons are greyed out with a reason.
- **Persona viewer** (top bar): read-only overview — stats, both L/M/S/U persona
  profiles, current 5-dimension relationship state with a 14-month trend, turning points.
- **Settings** (top bar): provider / base_url / model / max_tokens are written back to
  `config.yaml` (comment-preserving), and the key goes to the **Windows Credential
  Manager** (DPAPI-encrypted file as fallback) — never into `config.yaml`. "Save and test
  connection" verifies connectivity with a single minimal request.

## 2.2 Multiple friends (fully isolated)

The left "friends" panel creates / switches / renames / deletes friends. Each friend owns
an independent data directory:

```
data/profiles.json                # registry
data/profiles/<id>/ifwe_v1.db     # that friend's database (analysis + conversations)
data/profiles/<id>/persona/       # that friend's persona files
data/profiles/<id>/profile.yaml   # optional per-friend config overrides
```

Switching a friend switches the database, persona, conversations and sticker folder at
once. Switching/deleting is blocked (409) while an analysis or import is running.

> Upgrading an existing install: on first launch the current `data/` content is moved to
> `data/profiles/default/`, with a full copy backed up to `data_backup_<date>/`
> (gitignored) beforehand; a failed migration rolls back. Custom data directories set via
> `IFWE_DATA_DIR` are never migrated.

CLI equivalents: `python run.py profile list|new|use|rm`, `python run.py key set`.

## 3. Configure the LLM (unlocks message generation)

Replays are driven by an LLM. DeepSeek by default; any OpenAI-compatible
endpoint works:

```bash
# Option 1: in-app Settings panel (recommended on the desktop) — provider / base_url /
#           model / key, then "save and test connection". The key is stored in the
#           Windows Credential Manager, never in config.yaml.

# Option 2: environment variable (highest precedence; rename via config llm.api_key_env)
export LLM_API_KEY=sk-...            # Windows PowerShell: $env:LLM_API_KEY="sk-..."

# Option 3: edit the llm section in config.yaml (base_url/model/provider)
```

> CLI equivalents: `python run.py settings --model deepseek-chat`, `python run.py key set`

> Without a key you can still browse the UI, the timeline, the history replay, and run
> the offline analysis; sending a message will show a clear hint.

## 4. Import your own data

```bash
python run.py init          # creates config.yaml (first run)
python run.py init --source your_export.jsonl \
    --sender-a "your handle in the export" \
    --sender-b "their handle in the export"
python run.py analyze       # events / memories / state / turning points / personas
# No API key? python run.py analyze --skip-llm (heuristic events + persona templates)
```

Format details: [IMPORT_EN.md](IMPORT_EN.md).

## 5. Daily launch

```bash
python run.py server        # http://127.0.0.1:8015 (localhost only)
python run.py desktop       # native window (needs pip install pywebview)
python run.py server --profile ada       # pin a specific friend
```

The desktop shell (`desktop.py` / `IfWe.exe`) additionally: picks the next free port when
the preferred one is taken, enforces a single instance (a second launch opens the running
address in your browser), and exits cleanly when the window is closed (no orphan
processes). Packaging: see the "Packaging a release" section of README_EN.md.

Advanced CLI (optional):

```bash
python app/phase15_dial_engine.py new --start 2024-06-20 --divergence 2024-06-26 \
    --rewrite "I called instead of staying silent" --name "If I had called"
python app/phase15_dial_engine.py repl --line last
python app/phase15_dial_engine.py selftest      # offline end-to-end selftest
python app/phase4_retrieval.py --demo           # memory retrieval demo
```

## FAQ

**Q: Does `run.py demo` touch my real data?**
No. Demo artifacts live in `data_demo/`; real data lives in `data/`. They never mix.

**Q: Port already in use?**
`python run.py server --port 8088`, or change `defaults.port` in config.yaml; the desktop
shell probes upward automatically.

**Q: Where is the database? How do I back up?**
The active friend's `data/profiles/<id>/ifwe_v1.db` (or `data/ifwe_v1.db` when the
multi-friend registry is not in use; demo: `data_demo/ifwe_v1.db`). Backing up = copying
that file together with the sibling `persona/` folder.

**Q: Where exactly is my API key stored?**
Environment variable first (if set); otherwise the key saved in the UI goes to the Windows
Credential Manager (service `IfWe`, account = `llm.api_key_env`), falling back to
`data/.secret_llm_key` (DPAPI-encrypted, decryptable only by the current Windows user).
`config.yaml` and the logs never contain it in plaintext.

**Q: Offline analysis vs full analysis?**
Offline: keyword heuristics for events + empty persona templates (zero tokens; good for a
first look). Full: LLM extracts events per session and generates the four-layer persona
(consumes tokens; only sanitized text is sent).

**Q: Fresh start?**
Delete the current friend's data directory and run `init` again (your source JSONL is
untouched).
