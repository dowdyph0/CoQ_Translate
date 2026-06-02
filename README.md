# Caves of Qud — Translation Pipeline

Automated XML translation pipeline for [Caves of Qud](https://www.cavesofqud.com/) using a local LLM via [llama.cpp](https://github.com/ggerganov/llama.cpp). Translates every `▶`-marked string in `ExampleLanguage/*.example.xml` and writes the results as a ready-to-install language mod.

Tested with **Qwen3-14B-UD-Q4_K_XL** on an NVIDIA RTX 5060 Ti 16 GB.

> **Requires the `lang-experimental` Steam branch** — right-click the game in Steam → Properties → Betas → select `lang-experimental`.

---

## Requirements

- Python 3.11+ (or 3.10 with `pip install tomli`)
- A running [llama.cpp](https://github.com/ggml-org/llama.cpp) or any inference server with an OpenAI-compatible endpoint

---

## Setup

### 1. llama.cpp server

```bash
llama-server -m Qwen3-14B-UD-Q4_K_XL.gguf --port 9090 \
    -ngl 99 -np 4 --ctx-size 16384 --batch-size 512 \
    --flash-attn --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning-budget 0 --no-warmup
```

### 2. Python dependencies

```bash
pip install requests
```

### 3. Project layout

```text
CoQ_Translate/
├── translate.py
├── config.toml
├── prompts.toml
└── SpanishLanguage/                    ← generated mod (auto-created)
    ├── manifest.json
    ├── translation_cache.json
    ├── translation_memory.json
    ├── translation_failures.json
    └── languages/
        ├── languages.xml               ← language declaration for the game
        └── lang-es/
            ├── Strings.es.xml
            ├── Books.es.xml
            └── ...
```

---

## Configuration (`config.toml`)

```toml
llm_endpoint = "http://localhost:9090/v1"
model        = "local-model"

target_language = "Spanish"
lang_code       = "es"

mod_name         = "SpanishLanguage"
mod_display_name = "Español"
mod_author       = ""

temperature    = 0.6
top_k          = 20
top_p          = 0.95
repeat_penalty = 1.1
max_tokens     = 256

batch_size = 10
workers    = 4

streaming_assets_dir = 'C:\Program Files (x86)\Steam\steamapps\common\Caves of Qud\CoQ_Data\StreamingAssets'
```

| Key | Description |
| --- | ----------- |
| `lang_code` | Language code used in file names (`*.es.xml`) and `languages.xml` |
| `mod_name` | Output folder name — also the mod folder installed in `Mods/` |
| `mod_display_name` | Language name shown in the in-game language picker |
| `mod_author` | Optional — added to `manifest.json` if non-empty |
| `workers` | Parallel file workers — match llama.cpp `-np` value |
| `batch_size` | Strings per LLM call |
| `max_tokens` | Max tokens per LLM response |
| `streaming_assets_dir` | Path to the game's `StreamingAssets/` folder |

### Prompt tuning (`prompts.toml`)

Edit `prompts.toml` to adjust tone, add canonical glossary terms, or rewrite the system prompt — no need to touch `translate.py`. Keys: `tone_rules`, `sys_single`, `sys_batch`. `{lang}` is replaced at runtime with `target_language`.

---

## Usage

```bash
# Generate manifest.json and languages.xml only (no translation)
python translate.py --init-mod

# Translate all files
python translate.py

# Translate a single file
python translate.py --file Mutations

# Dry run — count strings without calling the LLM
python translate.py --dry-run

# Parallel workers
python translate.py --workers 4

# Build translation memory only, no XML output (safe to interrupt)
python translate.py --no-xml --workers 4

# Reconstruct XML files from translation_memory.json (no LLM calls)
python translate.py --rebuild

# Clear cache and retranslate everything
python translate.py --clear-cache
```

### Recommended workflow

```bash
# 1. Build translation memory (safe to interrupt and resume)
python translate.py --no-xml --workers 4

# 2. Review / edit translation_memory.json manually if needed

# 3. Generate all XML files from memory (fast, no LLM)
python translate.py --rebuild
```

---

## How it works

1. **Collect** — walks each `ExampleLanguage/*.example.xml` file and collects every `▶`-prefixed string (attributes, text nodes, rich text blocks)
2. **Cache check** — skips strings already translated using a scope-aware MD5 key (`lang:file:element:text`)
3. **Batch translate** — sends up to `batch_size` strings per LLM call; falls back to individual calls if the response is malformed
4. **Write** — updates the XML and saves cache and memory incrementally after each file
5. **Mod files** — generates `manifest.json` and `languages/languages.xml` automatically at the end of a full run

**Variable protection**: game variables (`=variable=`) and XML entities are replaced with `[[P0]]`, `[[P1]]`, … placeholders before the LLM call and restored afterwards.

---

## Output files

| File | Description |
| ---- | ----------- |
| `SpanishLanguage/languages/lang-es/*.es.xml` | Translated XML files |
| `SpanishLanguage/languages/languages.xml` | Language declaration read by the game |
| `SpanishLanguage/manifest.json` | Mod metadata |
| `SpanishLanguage/translation_cache.json` | MD5-keyed cache — skips already-translated strings on reruns |
| `SpanishLanguage/translation_memory.json` | Human-readable source↔translation pairs — edit then use `--rebuild` |
| `SpanishLanguage/translation_failures.json` | Strings that failed translation — review and retry |

---

## Installation

Copy the generated mod folder into the game's Mods directory:

```text
C:\Program Files (x86)\Steam\steamapps\common\Caves of Qud\CoQ_Data\StreamingAssets\Mods\SpanishLanguage\
```

Then launch the game (on the `lang-experimental` branch), select the Spanish language in the language selector, the game will restart and enjoy!.

---

## License

This project is a fan tool and is not affiliated with Freehold Games.
