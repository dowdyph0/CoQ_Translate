#!/usr/bin/env python3
"""
Caves of Qud Translation Pipeline v3
======================================
Reads ExampleLanguage/*.example.xml files, translates every ▶-marked string
using a local LLM (llama.cpp OpenAI-compatible endpoint), and writes results
to an output folder (default: Spanish/).

Improvements over v2
--------------------
* Scope-based cache keys — same text in different XML contexts gets a distinct
  cache entry, preventing wrong translations from leaking across contexts.
* Failure logging — every LLM error is printed to stderr in real time and
  written to translation_failures.json (overwritten on every run) for manual
  review and correction.

Usage
-----
    python translate.py                     # translate all files
    python translate.py --file Mutations    # single file
    python translate.py --dry-run           # count strings, no LLM calls
    python translate.py --clear-cache       # wipe cache and retranslate
    python translate.py --workers 4         # 4 parallel file workers

llama.cpp recommended flags (RTX 5060 Ti 16 GB)
------------------------------------------------
    llama-server -m model.gguf --port 8080 ^
        -ngl 99 -np 4 --ctx-size 2048 --batch-size 512

    -np 4          : 4 parallel completion slots (match --workers)
    --ctx-size 2048 : enough for a batch of 10 short strings at 512 tok/slot
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from typing import Optional
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # pip install tomli  (Python < 3.11)
    except ModuleNotFoundError:
        print("[ERROR] tomllib not found. Run: pip install tomli", file=sys.stderr)
        sys.exit(1)
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import xml.etree.ElementTree as ET

# ▶ prefix that marks translatable strings (U+25B6)
MARKER = "\u25b6"

# Maximum strings per batch LLM call (overridden by config.json "batch_size")
BATCH_SIZE = 20

# Strings longer than this are translated individually (book pages, long prose)
LONG_TEXT_THRESHOLD = 400

# ── Defaults / Config ─────────────────────────────────────────────────────────

@dataclass
class Config:
    llm_endpoint:         str            = "http://localhost:9090/v1"
    model:                str            = "local-model"
    target_language:      str            = "Spanish"
    lang_code:            str            = "es"
    temperature:          float          = 0.3
    max_tokens:           int            = 2048
    request_timeout:      int            = 120
    request_delay:        float          = 0.0
    max_retries:          int            = 3
    retry_delay:          int            = 5
    workers:              int            = 1
    batch_size:           int            = 20
    top_k:                Optional[int]   = None
    top_p:                Optional[float] = None
    min_p:                Optional[float] = None
    repeat_penalty:       Optional[float] = None
    streaming_assets_dir: Optional[Path] = None
    mod_name:             str            = "SpanishLanguage"
    mod_display_name:     str            = "Español"
    mod_author:           str            = ""
    # Prompts — loaded from prompts.toml
    tone_rules: str = ""
    sys_single: str = ""
    sys_batch:  str = ""
    # Derived paths — set by load_config
    example_dir: Path = Path()
    mod_dir:     Path = Path()
    output_dir:  Path = Path()
    cache_file:  Path = Path()


def load_config(script_dir: Path) -> Config:
    cfg = Config()
    cfg_path = script_dir / "config.toml"
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            content = f.read()
            # Strip UTF-8 BOM if present (written by some Windows editors)
            if content.startswith(b"\xef\xbb\xbf"):
                content = content[3:]
            raw = tomllib.loads(content.decode("utf-8"))
        for k, v in raw.items():
            if hasattr(cfg, k):
                val = Path(v) if k == "streaming_assets_dir" else v
                setattr(cfg, k, val)
    global BATCH_SIZE
    BATCH_SIZE = cfg.batch_size
    # Load prompts
    prompts_path = script_dir / "prompts.toml"
    if prompts_path.exists():
        with open(prompts_path, "rb") as f:
            p = tomllib.load(f)
        tone = p.get("tone_rules", "").strip()
        cfg.tone_rules = tone
        cfg.sys_single = p.get("sys_single", "").strip().replace("{tone_rules}", tone)
        cfg.sys_batch  = p.get("sys_batch",  "").strip().replace("{tone_rules}", tone)
    base_dir = cfg.streaming_assets_dir if cfg.streaming_assets_dir else script_dir
    work_dir = Path.cwd()
    cfg.example_dir = base_dir / "Base" / "ExampleLanguage"
    cfg.mod_dir     = work_dir / cfg.mod_name
    cfg.output_dir  = cfg.mod_dir / "languages" / f"lang-{cfg.lang_code}"
    cfg.cache_file  = cfg.mod_dir / "translation_cache.json"
    return cfg


# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict, path: Path, lock: threading.Lock) -> None:
    with lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)


# Attributes used to identify an XML element (first non-empty one wins)
_ID_ATTRS = ("Name", "ID", "Key")


def _elem_id(elem) -> str:
    """Return the first identifier attribute found on elem, or empty string."""
    if elem is None:
        return ""
    for attr in _ID_ATTRS:
        v = elem.get(attr, "")
        if v:
            return v
    return ""


def cache_key(lang_code: str, scope: str, text: str) -> str:
    """
    MD5 of 'lang:scope:text'.
    Scope encodes where in the XML the text lives, preventing the same English
    word from reusing a cached translation that was correct in a different context.
    """
    return hashlib.md5(f"{lang_code}:{scope}:{text}".encode()).hexdigest()


# ── Variable / Entity Protection ──────────────────────────────────────────────

_RE_GAME_VAR = re.compile(r"=[^=\n<>]{1,80}=")
_RE_XML_ENT  = re.compile(r"&#x?[0-9A-Fa-f]+;|&(?:amp|lt|gt|quot|apos);")


def protect(text: str) -> tuple:
    """Replace =variable= and XML entities with [[P0]], [[P1]], ..."""
    mapping: dict = {}
    counter = [0]

    def ph(original: str) -> str:
        key = f"[[P{counter[0]}]]"
        mapping[key] = original
        counter[0] += 1
        return key

    text = _RE_GAME_VAR.sub(lambda m: ph(m.group(0)), text)
    text = _RE_XML_ENT.sub(lambda m: ph(m.group(0)), text)
    return text, mapping


def restore(text: str, mapping: dict) -> str:
    for ph, orig in mapping.items():
        text = text.replace(ph, orig)
    return text


# ── Failure Logging ───────────────────────────────────────────────────────────

def _log_failure(
    failures,
    filename: str,
    kind: str,
    scope: str,
    key: str,
    text: str,
    error: str,
) -> None:
    """Print a [FAIL] line to stderr and append to failures list."""
    snippet = text[:60] + ("..." if len(text) > 60 else "")
    print(
        f"    [FAIL] {filename} | {scope} | \"{snippet}\" | key={key[:8]}",
        file=sys.stderr,
    )
    if failures is not None:
        failures.append({
            "file":  filename,
            "kind":  kind,
            "scope": scope,
            "key":   key,
            "text":  text,
            "error": error,
        })


# ── LLM Client ────────────────────────────────────────────────────────────────

def call_llm(text: str, config: "Config", *, batch: bool = False) -> str:
    system = (config.sys_batch if batch else config.sys_single).format(lang=config.target_language)
    payload = {
        "model":       config.model,
        "messages":    [
            {"role": "system", "content": system},
            {"role": "user",   "content": text},
        ],
        "temperature": config.temperature,
        "max_tokens":  config.max_tokens,
    }
    for attr in ("top_k", "top_p", "min_p", "repeat_penalty"):
        val = getattr(config, attr)
        if val is not None:
            payload[attr] = val
    max_retries = config.max_retries
    retry_delay = config.retry_delay
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{config.llm_endpoint}/chat/completions",
                json=payload,
                timeout=config.request_timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            if attempt < max_retries - 1:
                print(f"    [retry {attempt+1}/{max_retries}] {exc}", file=sys.stderr)
                time.sleep(retry_delay)
            else:
                raise


def _parse_batch_response(response: str, expected: int):
    """
    Parse '1. text\\n2. text\\n...' into a list of strings.
    Returns None if the count does not match expected.
    """
    lines = []
    for line in response.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^\d+\.\s*(.*)", stripped)
        if m:
            lines.append(m.group(1))
        else:
            return None  # unexpected line — bail out, fallback will handle it
    return lines if len(lines) == expected else None


# ── Batch Translation ─────────────────────────────────────────────────────────

def translate_batch(
    texts: list,
    cache: dict,
    config: dict,
    dry_run: bool,
    cache_lock: threading.Lock,
    scopes: list = None,
    failures: list = None,
    filename: str = "",
    records: list = None,
    memory_lookup: dict = None,
) -> list:
    """
    Translate a list of strings efficiently using scope-aware cache keys.
    scopes[i] is the scope string for texts[i]; pass None to use empty scope.
    failures is a shared list for failure records (thread-safe append only).
    records is a shared list that receives {file,scope,source,translation} entries.
    memory_lookup is a {(file,scope,source): translation} dict for --rebuild mode.
    """
    results = [None] * len(texts)
    miss_idx: list = []
    miss_data: list = []  # (protected, mapping, key, scope)

    for i, raw in enumerate(texts):
        text = raw.strip()
        if not text:
            results[i] = raw
            continue
        scope = scopes[i] if scopes else ""

        # ── Rebuild mode: use memory lookup, no LLM ───────────────────────────
        if memory_lookup is not None:
            mem_val = memory_lookup.get((filename, scope, text))
            results[i] = mem_val if mem_val is not None else raw
            continue

        protected, mapping = protect(text)
        key = cache_key(config.lang_code, scope, protected)
        with cache_lock:
            hit = cache.get(key)
        if hit is not None:
            results[i] = restore(hit, mapping)
            if records is not None:
                records.append({"file": filename, "scope": scope,
                                "source": text, "translation": results[i]})
        elif dry_run:
            snippet = text[:50] + ("..." if len(text) > 50 else "")
            results[i] = f"[TR: {snippet}]"
        else:
            miss_idx.append(i)
            miss_data.append((protected, mapping, key, scope))

    if not miss_idx or memory_lookup is not None:
        return results

    # Single uncached item — higher-quality single prompt
    if len(miss_idx) == 1:
        i = miss_idx[0]
        protected, mapping, key, scope = miss_data[0]
        try:
            translated = call_llm(protected, config, batch=False)
            with cache_lock:
                cache[key] = translated
            results[i] = restore(translated, mapping)
            if records is not None:
                records.append({"file": filename, "scope": scope,
                                "source": texts[i].strip(), "translation": results[i]})
        except Exception as exc:
            _log_failure(failures, filename, "single", scope, key, texts[i], str(exc))
            results[i] = texts[i]
        return results

    # Multiple uncached items — one batch call
    numbered = "\n".join(
        f"{n + 1}. {prot}" for n, (prot, _, _, _) in enumerate(miss_data)
    )
    try:
        response = call_llm(numbered, config, batch=True)
        parsed   = _parse_batch_response(response, len(miss_idx))
    except Exception as exc:
        print(f"    [ERROR] Batch LLM call failed: {exc}", file=sys.stderr)
        parsed = None

    if parsed is not None:
        for n, i in enumerate(miss_idx):
            protected, mapping, key, _scope = miss_data[n]
            translated = parsed[n].strip()
            with cache_lock:
                cache[key] = translated
            results[i] = restore(translated, mapping)
            if records is not None:
                records.append({"file": filename, "scope": _scope,
                                "source": texts[i].strip(), "translation": results[i]})
        return results

    # Fallback: translate individually
    print(
        f"    [WARN] Batch response malformed "
        f"(expected {len(miss_idx)} items). Falling back to individual calls.",
        file=sys.stderr,
    )
    for n, i in enumerate(miss_idx):
        protected, mapping, key, scope = miss_data[n]
        try:
            translated = call_llm(protected, config, batch=False)
            with cache_lock:
                cache[key] = translated
            results[i] = restore(translated, mapping)
            if records is not None:
                records.append({"file": filename, "scope": scope,
                                "source": texts[i].strip(), "translation": results[i]})
        except Exception as exc:
            _log_failure(failures, filename, "fallback", scope, key, texts[i], str(exc))
            results[i] = texts[i]

    return results


# ── Rich Text (Inline XML) Extraction ─────────────────────────────────────────

_RICH_TEXT_TAGS = frozenset({"p", "gametext"})
_CASE_TAGS      = frozenset({"case", "default"})

_BLOCK_PH_RE = re.compile(r"(\[\[P\d+\]\])")
_INLINE_SPLIT = re.compile(r"\[\[P(\d+)\]\]")


def _inline_text(elem) -> tuple:
    parts = [elem.text or ""]
    children = list(elem)
    for i, child in enumerate(children):
        parts.append(f"[[P{i}]]")
        parts.append(child.tail or "")
    return "".join(parts), children


def _restore_inline(translated: str, elem, children: list) -> None:
    parts = _INLINE_SPLIT.split(translated)
    elem.text = parts[0] if parts else ""
    for idx, child in enumerate(children):
        slot = idx * 2 + 2
        child.tail = parts[slot] if slot < len(parts) else ""


def _build_block_text(block_elem) -> tuple:
    global_map: dict = {}
    counter = [0]

    def make_ph_str(value: str) -> str:
        k = f"[[P{counter[0]}]]"
        global_map[k] = ("str", value)
        counter[0] += 1
        return k

    def make_ph_elem(child) -> str:
        k = f"[[P{counter[0]}]]"
        global_map[k] = ("elem", child)
        counter[0] += 1
        return k

    def protect_text(text: str) -> str:
        text = _RE_GAME_VAR.sub(lambda m: make_ph_str(m.group(0)), text)
        text = _RE_XML_ENT.sub(lambda m: make_ph_str(m.group(0)), text)
        return text

    paragraphs: list = []
    para_texts: list = []

    def walk(elem):
        tag = elem.tag if isinstance(elem.tag, str) else ""
        if tag in _RICH_TEXT_TAGS:
            parts = [protect_text(elem.text or "")]
            for child in elem:
                parts.append(make_ph_elem(child))
                parts.append(protect_text(child.tail or ""))
            text = "".join(parts).strip()
            if text:
                paragraphs.append(elem)
                para_texts.append(text)
        elif tag not in {"switch", *_CASE_TAGS}:
            for child in elem:
                walk(child)

    for child in block_elem:
        walk(child)

    return paragraphs, "\n\n".join(para_texts), global_map


def _restore_block_paragraph(tr_text: str, p_elem, global_map: dict) -> None:
    for child in list(p_elem):
        p_elem.remove(child)
    p_elem.text = ""

    parts = _BLOCK_PH_RE.split(tr_text)
    state = [None]

    def append_text(text: str) -> None:
        if state[0] is None:
            p_elem.text = (p_elem.text or "") + text
        else:
            state[0].tail = (state[0].tail or "") + text

    for part in parts:
        if not part:
            continue
        entry = global_map.get(part)
        if entry is None:
            append_text(part)
        elif entry[0] == "str":
            append_text(entry[1])
        else:
            child = entry[1]
            child.tail = ""
            p_elem.append(child)
            state[0] = child


def collect_case_atoms(block_elem) -> list:
    """
    Return a flat list of (elem, inline_children, raw_text, scope) for every
    <case>/<default> inside block_elem.  These are translated atomically.
    scope encodes the switch identity and case value for cache disambiguation.
    """
    results = []

    def walk_switch(switch_elem):
        switch_id = _elem_id(switch_elem)
        for child in switch_elem:
            if child.tag in _CASE_TAGS:
                text, inline_ch = _inline_text(child)
                raw = text.strip()
                if raw:
                    scope = f"case:{child.tag}:{switch_id}:{child.get('Value', '')}"
                    results.append((child, inline_ch, raw, scope))

    def walk(elem):
        tag = elem.tag if isinstance(elem.tag, str) else ""
        if tag == "switch":
            walk_switch(elem)
        for child in elem:
            walk(child)

    for child in block_elem:
        walk(child)
    return results


def translate_block(
    block_elem,
    cache: dict,
    config: dict,
    dry_run: bool,
    cache_lock: threading.Lock,
    scope: str = "",
    failures: list = None,
    filename: str = "",
    records: list = None,
    memory_lookup: dict = None,
) -> int:
    """
    Translate all <p>/<gametext> in block_elem as a single LLM call.
    Returns the number of paragraph strings processed.
    records receives one {file,scope,source,translation} entry per block.
    memory_lookup is a {(file,scope,source): translation} dict for --rebuild mode.
    """
    paragraphs, joined, global_map = _build_block_text(block_elem)
    if not paragraphs:
        return 0

    # ── Rebuild mode: use memory lookup, no LLM ───────────────────────────────
    if memory_lookup is not None:
        mem_val = memory_lookup.get((filename, scope, joined))
        if mem_val is None:
            return len(paragraphs)  # keep originals
        translated_joined = mem_val
        # fall through to restoration below
    else:
        key = cache_key(config.lang_code, scope, joined)
        with cache_lock:
            cached = cache.get(key)

        if cached is not None:
            translated_joined = cached
            if records is not None:
                records.append({"file": filename, "scope": scope,
                                "source": joined, "translation": translated_joined})
        elif dry_run:
            snippet = joined[:50] + ("..." if len(joined) > 50 else "")
            translated_joined = f"[TR: {snippet}]"
        else:
            try:
                translated_joined = call_llm(joined, config, batch=False)
                with cache_lock:
                    cache[key] = translated_joined
                if records is not None:
                    records.append({"file": filename, "scope": scope,
                                    "source": joined, "translation": translated_joined})
            except Exception as exc:
                _log_failure(failures, filename, "block", scope, key, joined[:200], str(exc))
                return len(paragraphs)  # keep originals on failure

    translated_parts = [p.strip() for p in translated_joined.split("\n\n") if p.strip()]

    for i, p_elem in enumerate(paragraphs):
        if i < len(translated_parts):
            _restore_block_paragraph(translated_parts[i], p_elem, global_map)

    if len(translated_parts) > len(paragraphs):
        for extra in translated_parts[len(paragraphs):]:
            new_p = ET.Element("p")
            for ph, entry in global_map.items():
                if entry[0] == "str":
                    extra = extra.replace(ph, entry[1])
            new_p.text = extra
            block_elem.append(new_p)

    return len(paragraphs)


# ── Job Collection ────────────────────────────────────────────────────────────

def collect_jobs(root) -> tuple:
    """
    Walk the element tree and collect all translatable items in one pass.

    Returns
    -------
    attr_jobs : list of [elem, attr_name, segments, is_translatable]
        Scope for each job is computed from elem at translation time.

    text_jobs : list of (elem, parent_elem, src_text)
        parent_elem is needed to build the scope string.

    rich_jobs : list of (elem, parent_elem)
        Elements whose text starts with MARKER and have child elements.
    """
    attr_jobs = []
    text_jobs  = []
    rich_jobs  = []

    def walk(elem, parent=None, inside_rich: bool = False):
        # ── Attributes ────────────────────────────────────────────────────────
        for attr_name, val in elem.attrib.items():
            if not val.startswith(MARKER):
                continue
            src = val[len(MARKER):]
            if "~" in src:
                raw_segs = src.split("~")
                segments = [raw_segs[0]]
                is_trans = [bool(raw_segs[0].strip())]
                for seg in raw_segs[1:]:
                    if seg.startswith(MARKER):
                        segments.append(seg[len(MARKER):])
                        is_trans.append(True)
                    else:
                        segments.append(seg)
                        is_trans.append(False)
                attr_jobs.append([elem, attr_name, segments, is_trans])
            else:
                attr_jobs.append([elem, attr_name, [src], [True]])

        # ── Element text ──────────────────────────────────────────────────────
        raw_text = (elem.text or "").lstrip()
        if raw_text.startswith(MARKER) and not inside_rich:
            after = raw_text[len(MARKER):]
            if list(elem):
                rich_jobs.append((elem, parent))
                for child in elem:
                    walk(child, parent=elem, inside_rich=True)
            else:
                src = after.strip()
                if src:
                    text_jobs.append((elem, parent, src))
                else:
                    elem.text = ""
            return

        for child in elem:
            walk(child, parent=elem, inside_rich=inside_rich)

    walk(root, parent=None)
    return attr_jobs, text_jobs, rich_jobs


# ── File Processing ───────────────────────────────────────────────────────────

def output_filename(example_name: str, lang_code: str = "es") -> str:
    base = example_name.replace(".example.xml", "")
    return f"{base}.{lang_code}.xml"


def process_file(
    example_path: Path,
    output_path: Path,
    cache: dict,
    config: dict,
    dry_run: bool,
    cache_lock: threading.Lock,
    memory_lookup: dict = None,
    write_xml: bool = True,
) -> dict:
    """Parse, collect, batch-translate, write back. Returns counter dict."""
    filename = example_path.name
    counters = {"total": 0, "file": filename, "failures": [], "records": []}
    failures = counters["failures"]
    records  = counters["records"]

    try:
        tree = ET.parse(str(example_path))
    except ET.ParseError as exc:
        print(f"  [ERROR] Cannot parse {filename}: {exc}", file=sys.stderr)
        counters["parse_error"] = True
        return counters

    root = tree.getroot()
    if "Lang" in root.attrib:
        root.set("Lang", config.lang_code)

    # ── Phase 1: Collect ──────────────────────────────────────────────────────
    attr_jobs, text_jobs, rich_jobs = collect_jobs(root)

    # ── Phase 2a: Translate attributes ───────────────────────────────────────
    flat_texts:  list = []
    flat_scopes: list = []
    flat_map:    list = []  # (job_idx, seg_idx, flat_idx)

    for job_idx, job in enumerate(attr_jobs):
        elem, attr_name, segments, is_trans = job
        scope = f"attr:{attr_name}:{elem.tag}:{_elem_id(elem)}"
        for seg_idx, (seg, should_tr) in enumerate(zip(segments, is_trans)):
            if should_tr and seg.strip():
                flat_map.append((job_idx, seg_idx, len(flat_texts)))
                flat_texts.append(seg)
                flat_scopes.append(scope)

    attr_translated: list = []
    for start in range(0, len(flat_texts), BATCH_SIZE):
        batch        = flat_texts[start : start + BATCH_SIZE]
        batch_scopes = flat_scopes[start : start + BATCH_SIZE]
        attr_translated.extend(
            translate_batch(
                batch, cache, config, dry_run, cache_lock,
                scopes=batch_scopes, failures=failures, filename=filename,
                records=records, memory_lookup=memory_lookup,
            )
        )

    counters["total"] += len(flat_texts)

    for job_idx, seg_idx, flat_idx in flat_map:
        t = attr_translated[flat_idx]
        if t is not None:
            attr_jobs[job_idx][2][seg_idx] = t

    for _elem, attr_name, segments, _is_trans in attr_jobs:
        _elem.attrib[attr_name] = "~".join(segments)

    # ── Phase 2b: Translate simple text elements ──────────────────────────────
    short_jobs = [(e, p, s) for e, p, s in text_jobs if len(s) <= LONG_TEXT_THRESHOLD]
    long_jobs  = [(e, p, s) for e, p, s in text_jobs if len(s)  > LONG_TEXT_THRESHOLD]

    if short_jobs:
        short_texts  = [s for _, _, s in short_jobs]
        short_scopes = [
            f"text:{e.tag}:{p.tag if p is not None else 'root'}:{_elem_id(p)}"
            for e, p, _ in short_jobs
        ]
        short_results: list = []
        for start in range(0, len(short_texts), BATCH_SIZE):
            batch        = short_texts[start : start + BATCH_SIZE]
            batch_scopes = short_scopes[start : start + BATCH_SIZE]
            short_results.extend(
                translate_batch(
                    batch, cache, config, dry_run, cache_lock,
                    scopes=batch_scopes, failures=failures, filename=filename,
                    records=records, memory_lookup=memory_lookup,
                )
            )
        counters["total"] += len(short_jobs)
        for (elem, _, _), translated in zip(short_jobs, short_results):
            if translated is not None:
                elem.text = translated

    for elem, parent, src in long_jobs:
        scope = f"text:{elem.tag}:{parent.tag if parent is not None else 'root'}:{_elem_id(parent)}"
        res = translate_batch(
            [src], cache, config, dry_run, cache_lock,
            scopes=[scope], failures=failures, filename=filename,
            records=records, memory_lookup=memory_lookup,
        )
        if res[0] is not None:
            elem.text = res[0]
        counters["total"] += 1

    # ── Phase 2c: Translate rich text blocks ──────────────────────────────────
    case_atoms: list = []
    rich_total  = 0

    for rich_elem, parent_elem in rich_jobs:
        scope = (
            f"block:{rich_elem.tag}"
            f":{parent_elem.tag if parent_elem is not None else 'root'}"
            f":{_elem_id(parent_elem)}"
        )
        rich_total += translate_block(
            rich_elem, cache, config, dry_run, cache_lock,
            scope=scope, failures=failures, filename=filename,
            records=records, memory_lookup=memory_lookup,
        )
        case_atoms.extend(collect_case_atoms(rich_elem))

    counters["total"] += rich_total

    # Batch-translate case/default atoms
    if case_atoms:
        case_texts  = [raw   for _, _, raw,  _     in case_atoms]
        case_scopes = [scope for _, _, _,    scope in case_atoms]
        case_results: list = []
        for start in range(0, len(case_texts), BATCH_SIZE):
            batch        = case_texts[start  : start + BATCH_SIZE]
            batch_scopes = case_scopes[start : start + BATCH_SIZE]
            case_results.extend(
                translate_batch(
                    batch, cache, config, dry_run, cache_lock,
                    scopes=batch_scopes, failures=failures, filename=filename,
                    records=records, memory_lookup=memory_lookup,
                )
            )
        counters["total"] += len(case_atoms)
        for (node_elem, node_children, _raw, _scope), translated in zip(case_atoms, case_results):
            if translated is not None:
                _restore_inline(translated, node_elem, node_children)

    # Strip the marker from rich elements' own .text
    for rich_elem, _parent in rich_jobs:
        if rich_elem.text and MARKER in rich_elem.text:
            cleaned = rich_elem.text.replace(MARKER, "").strip()
            rich_elem.text = cleaned if cleaned else None

    # ── Phase 3: Write output file ────────────────────────────────────────────
    if write_xml and not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        xml_body = ET.tostring(root, encoding="unicode")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write(xml_body)
            f.write("\n")

    return counters


# ── Mod File Generation ──────────────────────────────────────────────────────

def generate_mod_files(config: "Config") -> None:
    """Generate languages.xml and manifest.json for the language mod."""
    languages_dir = config.mod_dir / "languages"
    languages_dir.mkdir(parents=True, exist_ok=True)
    with open(languages_dir / "languages.xml", "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write('<languages Encoding="utf-8">\n')
        f.write(f'  <lang Code="{config.lang_code}" DisplayName="{config.mod_display_name}" />\n')
        f.write('</languages>\n')
    manifest = {
        "id": f"coq-{config.lang_code}-translation",
        "title": f"Caves of Qud {config.target_language} Translation",
        "description": f"Caves of Qud {config.target_language} translation mod.",
        "version": "1.0.0",
        "tags": "Localization",
    }
    if config.mod_author:
        manifest["author"] = config.mod_author
    with open(config.mod_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=4)
    print(f"Mod     : {config.mod_dir / 'manifest.json'}")
    print(f"         {languages_dir / 'languages.xml'}")


# ── Main ──────────────────────────────────────────────────────────────────────

def _load_memory(path: Path) -> dict:
    """Load translation_memory.json into a {(file,scope,source): translation} dict."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("entries", []) if isinstance(data, dict) else data
    return {(e["file"], e["scope"], e["source"]): e["translation"] for e in entries}


def _save_memory(
    memory_path: Path,
    existing: dict,
    new_records: list,
    lang: str,
    lang_code: str,
) -> int:
    """
    Merge new_records into existing, then write translation_memory.json.
    Returns the total number of entries written.
    """
    # existing is {(file,scope,source): translation}; merge new records on top
    merged = dict(existing)
    for rec in new_records:
        key = (rec["file"], rec["scope"], rec["source"])
        merged[key] = rec["translation"]
    entries = [
        {"file": f, "scope": sc, "source": sr, "translation": tr}
        for (f, sc, sr), tr in sorted(merged.items())
    ]
    payload = {"language": lang, "lang_code": lang_code, "entries": entries}
    with open(memory_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return len(entries)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Caves of Qud XML translation pipeline v3 (llama.cpp backend)"
    )
    parser.add_argument(
        "--file", metavar="NAME",
        help="Translate a single file, e.g. --file Mutations",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count translatable strings without calling the LLM",
    )
    parser.add_argument(
        "--clear-cache", action="store_true",
        help="Delete the translation cache before running",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help=(
            "Reconstruct translated XML files from translation_memory.json "
            "without calling the LLM. Edit the memory file first, then run "
            "--rebuild to apply your corrections."
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Parallel file workers (overrides config.json; should match llama.cpp -np)",
    )
    parser.add_argument(
        "--no-xml", action="store_true",
        help=(
            "Translate and update translation_memory.json only, skip XML output. "
            "Use --rebuild afterwards to generate the XML files."
        ),
    )
    parser.add_argument(
        "--init-mod", action="store_true",
        help="Create manifest.json and languages/languages.xml for the mod, then exit.",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()
    config     = load_config(script_dir)
    if args.workers is not None:
        config.workers = args.workers

    # ── Init-mod mode ─────────────────────────────────────────────────────────
    if args.init_mod:
        generate_mod_files(config)
        return

    failures_path = config.cache_file.parent / "translation_failures.json"
    memory_path   = config.cache_file.parent / "translation_memory.json"

    if not config.example_dir.exists():
        print(f"[ERROR] ExampleLanguage dir not found: {config.example_dir}", file=sys.stderr)
        sys.exit(1)

    # ── Rebuild mode ──────────────────────────────────────────────────────────
    if args.rebuild:
        memory_lookup = _load_memory(memory_path)
        if not memory_lookup:
            print(
                f"[ERROR] translation_memory.json not found or empty: {memory_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"[REBUILD] Reconstructing XML from {len(memory_lookup)} memory entries\n"
            f"  Memory : {memory_path}\n"
            f"  Output : {config.output_dir}\n"
        )
        files = (
            [config.example_dir / (args.file if args.file.endswith(".example.xml")
                            else args.file + ".example.xml")]
            if args.file else sorted(config.example_dir.glob("*.example.xml"))
        )
        total_strings = 0
        empty_cache   = {}
        empty_lock    = threading.Lock()
        for example_path in files:
            out_name = output_filename(example_path.name, config.lang_code)
            out_path = config.output_dir / out_name
            print(f"  {example_path.name}  ->  {out_name}")
            result = process_file(
                example_path, out_path, empty_cache, config,
                dry_run=False, cache_lock=empty_lock,
                memory_lookup=memory_lookup,
            )
            n = result["total"]
            total_strings += n
            print(f"    {n} string(s)")
        print(f"\nDone. {total_strings} total string(s) processed.")
        return

    if args.clear_cache and config.cache_file.exists():
        config.cache_file.unlink()
        print("[INFO] Cache cleared.")

    cache      = load_cache(config.cache_file)
    cache_lock = threading.Lock()

    if args.file:
        name   = args.file if args.file.endswith(".example.xml") else args.file + ".example.xml"
        target = config.example_dir / name
        if not target.exists():
            print(f"[ERROR] File not found: {target}", file=sys.stderr)
            sys.exit(1)
        files = [target]
    else:
        files = sorted(config.example_dir.glob("*.example.xml"))

    if not files:
        print("[WARN] No .example.xml files found.", file=sys.stderr)
        sys.exit(0)

    mode_label = "[DRY-RUN] " if args.dry_run else ""
    print(
        f"{mode_label}Translating {len(files)} file(s)  ->  {config.output_dir}\n"
        f"  LLM      : {config.llm_endpoint}  model={config.model}\n"
        f"  Language : {config.target_language} ({config.lang_code})\n"
        f"  Workers  : {config.workers}\n"
        f"  Batch    : {BATCH_SIZE} strings/call\n"
    )

    total_strings = 0
    all_failures: list = []
    all_records:  list = []

    existing_memory = _load_memory(memory_path)

    if config.workers == 1:
        # ── Sequential ────────────────────────────────────────────────────────
        for example_path in files:
            out_name = output_filename(example_path.name, config.lang_code)
            out_path = config.output_dir / out_name
            print(f"  {example_path.name}  ->  {out_name}")
            result = process_file(
                example_path, out_path, cache, config, args.dry_run, cache_lock,
                write_xml=not args.no_xml,
            )
            n = result["total"]
            total_strings += n
            all_failures.extend(result.get("failures", []))
            all_records.extend(result.get("records", []))
            print(f"    {n} string(s)")
            if not args.dry_run:
                save_cache(cache, config.cache_file, cache_lock)
                existing_memory = _load_memory(memory_path)
                _save_memory(
                    memory_path, existing_memory, all_records,
                    config.target_language, config.lang_code,
                )
                all_records = []

    else:
        # ── Parallel ──────────────────────────────────────────────────────────
        def do_file(example_path: Path) -> tuple:
            out_name = output_filename(example_path.name, config.lang_code)
            out_path = config.output_dir / out_name
            result   = process_file(
                example_path, out_path, cache, config, args.dry_run, cache_lock,
                write_xml=not args.no_xml,
            )
            n     = result["total"]
            fails = result.get("failures", [])
            recs  = result.get("records", [])
            return f"  {example_path.name}  ->  {out_name}  [{n} strings]", n, fails, recs

        completed = 0
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            futures = {pool.submit(do_file, p): p for p in files}
            for future in as_completed(futures):
                try:
                    label, n, fails, recs = future.result()
                    print(label)
                    total_strings += n
                    all_failures.extend(fails)
                    all_records.extend(recs)
                    completed += 1
                    if not args.dry_run and completed % 5 == 0:
                        save_cache(cache, config.cache_file, cache_lock)
                        existing_memory = _load_memory(memory_path)
                        _save_memory(
                            memory_path, existing_memory, all_records,
                            config.target_language, config.lang_code,
                        )
                        all_records = []
                except Exception as exc:
                    print(f"  [ERROR] {futures[future].name}: {exc}", file=sys.stderr)
        if not args.dry_run:
            save_cache(cache, config.cache_file, cache_lock)

    print(f"\nDone. {total_strings} total string(s) processed.")

    if not args.dry_run:
        print(f"Output : {config.output_dir}")
        print(f"Cache  : {config.cache_file}")

        # Write failures file — always overwrite, even if empty
        with open(failures_path, "w", encoding="utf-8") as f:
            json.dump(all_failures, f, ensure_ascii=False, indent=2)

        if all_failures:
            print(
                f"\nWARN: {len(all_failures)} string(s) failed translation. "
                f"See: {failures_path}",
                file=sys.stderr,
            )
        else:
            print(f"Failures: 0")

        # Write / update translation memory (flush any remaining records)
        existing_memory = _load_memory(memory_path)
        n_entries = _save_memory(
            memory_path, existing_memory, all_records,
            config.target_language, config.lang_code,
        )
        print(f"Memory : {memory_path}  ({n_entries} entries)")

        generate_mod_files(config)


if __name__ == "__main__":
    main()
