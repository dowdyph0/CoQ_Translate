"""
Diagnóstico: encuentra los 345 strings que export_xml no puede aplicar.
Ejecutar con: python3 diag_missing.py
(desde el directorio raíz del proyecto, con el entorno Django disponible)
"""

import os
import sys
import django
import xml.etree.ElementTree as ET
from pathlib import Path

# --- Django setup
sys.path.insert(0, str(Path(__file__).parent / "editor"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "editor_project.settings")
django.setup()

from translations.models import Language, TranslationEntry, _source_hash

# --- translate_db helpers
sys.path.insert(0, str(Path(__file__).parent))
from translate_db import MARKER, collect_jobs, collect_case_atoms, _elem_id, _build_block_text

EXAMPLE_DIR = Path(os.environ.get("EXAMPLE_LANGUAGE_DIR", "/example-language"))

# --- Build db_cache exactly as export_xml does
lang = Language.objects.get(name="Spanish")
entries = (
    TranslationEntry.objects
    .filter(language=lang)
    .exclude(status=TranslationEntry.STATUS_FAILED)
    .select_related("source_file")
    .values_list("source_file__name", "scope", "source_hash", "translation")
)
db_cache = {(sf, sc, sh): tr for sf, sc, sh, tr in entries}
print(f"DB cache size: {len(db_cache)}")

# --- Scan XMLs exactly as export_xml does and find misses
target_files = ["EmbarkModules.example.xml", "Quests.example.xml", "Worlds.example.xml"]

# How many DB entries per file?
print("\n=== DB entries per file ===")
for fname in target_files:
    n = TranslationEntry.objects.filter(language=lang, source_file__name=fname).exclude(status="failed").count()
    n_empty = TranslationEntry.objects.filter(language=lang, source_file__name=fname, translation="").exclude(status="failed").count()
    print(f"  {fname}: {n} entries ({n_empty} empty translation)")

# Check if db_cache has duplicate keys (dict is smaller than queryset)
all_entries = list(
    TranslationEntry.objects
    .filter(language=lang)
    .exclude(status=TranslationEntry.STATUS_FAILED)
    .select_related("source_file")
    .only("source_file__name", "scope", "source_hash", "translation")
)
db_cache2 = {}
dup_count = 0
for e in all_entries:
    k = (e.source_file.name, e.scope, e.source_hash)
    if k in db_cache2:
        dup_count += 1
    db_cache2[k] = e.translation
print(f"\nDB total entries (non-failed): {len(all_entries)}, unique keys: {len(db_cache2)}, duplicates overwritten: {dup_count}")

print("\n=== Checking each file against db_cache2 (built from ORM objects like export_xml) ===")
for fname in target_files:
    fpath = EXAMPLE_DIR / fname
    if not fpath.exists():
        print(f"NOT FOUND: {fpath}")
        continue
    tree = ET.parse(str(fpath))
    root = tree.getroot()
    attr_jobs, text_jobs, rich_jobs = collect_jobs(root)

    applied = 0
    total = 0
    missed_detail = []

    # Attributes (exact copy of export_xml logic)
    for job in attr_jobs:
        elem, attr_name, segments, is_trans = job
        scope = f"attr:{attr_name}:{elem.tag}:{_elem_id(elem)}"
        for seg_idx, (seg, should_tr) in enumerate(zip(segments, is_trans)):
            if should_tr and seg.strip():
                sh = _source_hash(seg)
                t = db_cache.get((fname, scope, sh))
                if t is not None:
                    applied += 1
                else:
                    missed_detail.append(("attr", scope, seg[:50]))
                total += 1

    # Text (exact copy)
    for elem, parent, src in text_jobs:
        scope = (
            f"text:{elem.tag}"
            f":{parent.tag if parent is not None else 'root'}"
            f":{_elem_id(parent) or _elem_id(elem)}"
        )
        sh = _source_hash(src)
        t = db_cache.get((fname, scope, sh))
        if t is not None:
            applied += 1
        else:
            missed_detail.append(("text", scope, src[:50]))
        total += 1

    # Rich blocks (exact copy)
    from translate_db import _restore_block_paragraph, _restore_inline
    for rich_elem, parent_elem in rich_jobs:
        scope = (
            f"block:{rich_elem.tag}"
            f":{parent_elem.tag if parent_elem is not None else 'root'}"
            f":{_elem_id(parent_elem)}"
        )
        paragraphs, joined, global_map = _build_block_text(rich_elem)
        if paragraphs:
            sh = _source_hash(joined)
            t = db_cache.get((fname, scope, sh))
            total += 1
            if t is not None:
                applied += 1
            else:
                missed_detail.append(("block", scope, joined[:50]))

        for child_elem, inline_ch, raw, atom_scope in collect_case_atoms(rich_elem):
            sh = _source_hash(raw)
            t = db_cache.get((fname, atom_scope, sh))
            total += 1
            if t is not None:
                applied += 1
            else:
                missed_detail.append(("case", atom_scope, raw[:50]))

    print(f"\n{fname}: phase2 total={total}  applied={applied}  missed={len(missed_detail)}")
    for kind, scope, snippet in missed_detail[:10]:
        print(f"  [MISS-{kind}] scope={scope!r}  src={snippet!r}")
    if len(missed_detail) > 10:
        print(f"  ... and {len(missed_detail)-10} more")

print("\n=== PHASE 1 (original) ===")
for fname in target_files:
    fpath = EXAMPLE_DIR / fname
    if not fpath.exists():
        print(f"NOT FOUND: {fpath}")
        continue

    tree = ET.parse(str(fpath))
    root = tree.getroot()
    attr_jobs, text_jobs, rich_jobs = collect_jobs(root)

    total = 0
    in_cache = 0
    null_translation = 0
    misses = []

    for job in attr_jobs:
        elem, attr_name, segments, is_trans = job
        scope = f"attr:{attr_name}:{elem.tag}:{_elem_id(elem)}"
        for seg, should_tr in zip(segments, is_trans):
            if should_tr and seg.strip():
                total += 1
                sh = _source_hash(seg)
                t = db_cache.get((fname, scope, sh))
                if t is None:
                    misses.append(("attr", scope, seg[:60]))
                else:
                    in_cache += 1
                    if t == "":
                        null_translation += 1

    for elem, parent, src in text_jobs:
        scope = (
            f"text:{elem.tag}"
            f":{parent.tag if parent is not None else 'root'}"
            f":{_elem_id(parent) or _elem_id(elem)}"
        )
        total += 1
        sh = _source_hash(src)
        t = db_cache.get((fname, scope, sh))
        if t is None:
            misses.append(("text", scope, src[:60]))
        else:
            in_cache += 1
            if t == "":
                null_translation += 1

    for rich_elem, parent_elem in rich_jobs:
        scope = (
            f"block:{rich_elem.tag}"
            f":{parent_elem.tag if parent_elem is not None else 'root'}"
            f":{_elem_id(parent_elem)}"
        )
        paragraphs, joined, _ = _build_block_text(rich_elem)
        if paragraphs:
            total += 1
            sh = _source_hash(joined)
            t = db_cache.get((fname, scope, sh))
            if t is None:
                misses.append(("block", scope, joined[:60]))
            else:
                in_cache += 1
                if t == "":
                    null_translation += 1

        for child_elem, inline_ch, raw, atom_scope in collect_case_atoms(rich_elem):
            total += 1
            sh = _source_hash(raw)
            t = db_cache.get((fname, atom_scope, sh))
            if t is None:
                misses.append(("case", atom_scope, raw[:60]))
            else:
                in_cache += 1
                if t == "":
                    null_translation += 1

    print(f"\n=== {fname} ===")
    print(f"  diag total={total}  in_cache={in_cache}  misses={len(misses)}  empty_translation={null_translation}")
    for kind, scope, snippet in misses[:20]:
        print(f"  [MISS-{kind}] scope={scope!r}  src={snippet!r}")
    if len(misses) > 20:
        print(f"  ... and {len(misses)-20} more misses")
