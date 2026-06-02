"""
pipeline.py — XML parsing and LLM translation helpers.

Shared by the management commands scan_xml, translate_pending, export_xml.
Config is read from django.conf.settings (populated via .env / Docker env_file).
"""

import re
import time

import requests
import xml.etree.ElementTree as ET

# ── Constants ─────────────────────────────────────────────────────────────────

MARKER = "\u25b6"  # ▶ prefix that marks translatable strings
LONG_TEXT_THRESHOLD = 400

_ID_ATTRS       = ("Name", "ID", "Key")
_RICH_TEXT_TAGS = frozenset({"p", "gametext"})
_CASE_TAGS      = frozenset({"case", "default"})
_BLOCK_PH_RE    = re.compile(r"(\[\[P\d+\]\])")
_INLINE_SPLIT   = re.compile(r"\[\[P(\d+)\]\]")
_RE_GAME_VAR    = re.compile(r"=[^=\n<>]{1,80}=")
_RE_XML_ENT     = re.compile(r"&#x?[0-9A-Fa-f]+;|&(?:amp|lt|gt|quot|apos);")


# ── Variable / Entity Protection ──────────────────────────────────────────────

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


# ── LLM Client ────────────────────────────────────────────────────────────────

def call_llm(text: str, *, batch: bool = False) -> str:
    """Call the LLM endpoint. Reads all config from django.conf.settings."""
    from django.conf import settings

    system = (settings.LLM_SYS_BATCH if batch else settings.LLM_SYS_SINGLE).format(
        lang=settings.TARGET_LANGUAGE
    )
    payload = {
        "model":       settings.LLM_MODEL,
        "messages":    [
            {"role": "system", "content": system},
            {"role": "user",   "content": text},
        ],
        "temperature": settings.TEMPERATURE,
        "max_tokens":  settings.MAX_TOKENS,
    }
    for attr in ("TOP_K", "TOP_P", "MIN_P", "REPEAT_PENALTY"):
        val = getattr(settings, attr)
        if val is not None:
            payload[attr.lower()] = val

    for attempt in range(settings.MAX_RETRIES):
        try:
            resp = requests.post(
                f"{settings.LLM_ENDPOINT}/chat/completions",
                json=payload,
                timeout=settings.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            if attempt < settings.MAX_RETRIES - 1:
                print(f"    [retry {attempt + 1}/{settings.MAX_RETRIES}] {exc}")
                time.sleep(settings.RETRY_DELAY)
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
            return None
    return lines if len(lines) == expected else None


# ── XML Helpers ───────────────────────────────────────────────────────────────

def _elem_id(elem) -> str:
    """Return the first identifier attribute found on elem, or empty string."""
    if elem is None:
        return ""
    for attr in _ID_ATTRS:
        v = elem.get(attr, "")
        if v:
            return v
    return ""


def collect_jobs(root) -> tuple:
    """
    Walk the element tree and collect all translatable items in one pass.

    Returns
    -------
    attr_jobs : list of [elem, attr_name, segments, is_translatable]
    text_jobs : list of (elem, parent_elem, src_text)
    rich_jobs : list of (elem, parent_elem)
    """
    attr_jobs = []
    text_jobs = []
    rich_jobs = []

    def walk(elem, parent=None, inside_rich: bool = False):
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
    <case>/<default> inside block_elem.
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
