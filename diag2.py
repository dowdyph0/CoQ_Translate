"""
Diagnóstico 2: compara db_cache de values_list vs ORM .only() como export_xml.
python3 /app/diag2.py
"""
import os, sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "editor"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "editor_project.settings")
import django; django.setup()

from translations.models import Language, TranslationEntry, _source_hash
sys.path.insert(0, str(Path(__file__).parent))
from translate_db import collect_jobs, collect_case_atoms, _elem_id, _build_block_text

EXAMPLE_DIR = Path(os.environ.get("EXAMPLE_LANGUAGE_DIR", "/example-language"))
lang = Language.objects.get(name="Spanish")

# ── cache A: values_list (diagnostic anterior) ────────────────────────────────
qs = (TranslationEntry.objects.filter(language=lang)
      .exclude(status=TranslationEntry.STATUS_FAILED)
      .select_related("source_file")
      .values_list("source_file__name", "scope", "source_hash", "translation"))
cache_A = {(sf, sc, sh): tr for sf, sc, sh, tr in qs}

# ── cache B: ORM .only() exactamente como export_xml ─────────────────────────
qs2 = (TranslationEntry.objects.filter(language=lang)
       .exclude(status=TranslationEntry.STATUS_FAILED)
       .select_related("source_file")
       .only("source_file__name", "scope", "source_hash", "translation"))
cache_B = {(e.source_file.name, e.scope, e.source_hash): e.translation for e in qs2}

print(f"cache_A size: {len(cache_A)}")
print(f"cache_B size: {len(cache_B)}")
diff_AB = set(cache_A.keys()) - set(cache_B.keys())
diff_BA = set(cache_B.keys()) - set(cache_A.keys())
print(f"Keys in A not in B: {len(diff_AB)}")
print(f"Keys in B not in A: {len(diff_BA)}")
if diff_AB:
    for k in list(diff_AB)[:5]:
        print(f"  A-only: {k}")
if diff_BA:
    for k in list(diff_BA)[:5]:
        print(f"  B-only: {k}")

# ── scan the 3 problem files using cache_B ────────────────────────────────────
target_files = ["EmbarkModules.example.xml", "Quests.example.xml", "Worlds.example.xml"]
print()
for fname in target_files:
    fpath = EXAMPLE_DIR / fname
    tree = ET.parse(str(fpath))
    root = tree.getroot()
    attr_jobs, text_jobs, rich_jobs = collect_jobs(root)

    total = applied_A = applied_B = 0
    misses_B = []

    for job in attr_jobs:
        elem, attr_name, segments, is_trans = job
        scope = f"attr:{attr_name}:{elem.tag}:{_elem_id(elem)}"
        for seg, should_tr in zip(segments, is_trans):
            if should_tr and seg.strip():
                sh = _source_hash(seg); total += 1
                if cache_A.get((fname, scope, sh)) is not None: applied_A += 1
                tB = cache_B.get((fname, scope, sh))
                if tB is not None: applied_B += 1
                else: misses_B.append(("attr", scope, seg[:50]))

    for elem, parent, src in text_jobs:
        scope = (f"text:{elem.tag}:{parent.tag if parent is not None else 'root'}"
                 f":{_elem_id(parent) or _elem_id(elem)}")
        sh = _source_hash(src); total += 1
        if cache_A.get((fname, scope, sh)) is not None: applied_A += 1
        tB = cache_B.get((fname, scope, sh))
        if tB is not None: applied_B += 1
        else: misses_B.append(("text", scope, src[:50]))

    for rich_elem, parent_elem in rich_jobs:
        scope = (f"block:{rich_elem.tag}:{parent_elem.tag if parent_elem is not None else 'root'}"
                 f":{_elem_id(parent_elem)}")
        paragraphs, joined, _ = _build_block_text(rich_elem)
        if paragraphs:
            sh = _source_hash(joined); total += 1
            if cache_A.get((fname, scope, sh)) is not None: applied_A += 1
            tB = cache_B.get((fname, scope, sh))
            if tB is not None: applied_B += 1
            else: misses_B.append(("block", scope, joined[:50]))
        for child_elem, inline_ch, raw, atom_scope in collect_case_atoms(rich_elem):
            sh = _source_hash(raw); total += 1
            if cache_A.get((fname, atom_scope, sh)) is not None: applied_A += 1
            tB = cache_B.get((fname, atom_scope, sh))
            if tB is not None: applied_B += 1
            else: misses_B.append(("case", atom_scope, raw[:50]))

    print(f"{fname}: total={total} applied_A={applied_A} applied_B={applied_B} misses_B={len(misses_B)}")
    for kind, scope, snippet in misses_B[:5]:
        print(f"  [MISS-B-{kind}] scope={scope!r}  src={snippet!r}")
