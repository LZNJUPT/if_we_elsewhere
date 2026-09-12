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

## 3. Configure the LLM (unlocks message generation)

Replays are driven by an LLM. DeepSeek by default; any OpenAI-compatible
endpoint works:

```bash
# Option 1: environment variable (recommended; rename via config llm.api_key_env)
export LLM_API_KEY=sk-...            # Windows PowerShell: $env:LLM_API_KEY="sk-..."

# Option 2: edit the llm section in config.yaml (base_url/model/provider);
#           the key itself still comes from an environment variable
```

> Without a key you can still browse the UI, the timeline and the history
> replay; sending a message will show a clear hint.

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
```

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
`python run.py server --port 8088`, or change `defaults.port` in config.yaml.

**Q: Where is the database? How do I back up?**
`data/ifwe_v1.db` (demo: `data_demo/ifwe_v1.db`). Backing up = copying the file.

**Q: Fresh start?**
Delete `data/` and run `init` again (your source JSONL is untouched).
