"""
export_xml — rebuild .es.xml files directly from the DB.

For every .example.xml found in the game's ExampleLanguage folder, each
translatable string is looked up in the DB and written to the corresponding
.es.xml output file.  Strings that are not yet in the DB are left in English.

Usage:
    python manage.py export_xml
    python manage.py export_xml --language Spanish
    python manage.py export_xml --language-id 1
    python manage.py export_xml --example-dir /path/to/ExampleLanguage
    python manage.py export_xml --output-dir /path/to/lang-es
"""

import os
import xml.etree.ElementTree as ET
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from translations.models import Language, TranslationEntry, _source_hash
from translations.pipeline import (
    MARKER,
    collect_jobs,
    collect_case_atoms,
    _elem_id,
    _restore_inline,
    _build_block_text,
    _restore_block_paragraph,
)


# ── Core export logic ─────────────────────────────────────────────────────────

def _apply_translations(
    example_path: Path,
    output_path: Path,
    db_cache: dict,
    lang_code: str,
) -> dict:
    """
    Parse example_path, substitute translations from db_cache, write output_path.

    db_cache maps (filename, scope, source_hash) → translated_text.
    Returns {"total": N, "applied": M, "file": name}.
    """
    filename = example_path.name
    counters = {"total": 0, "applied": 0, "file": filename}

    try:
        tree = ET.parse(str(example_path))
    except ET.ParseError as exc:
        raise CommandError(f"Cannot parse {filename}: {exc}")

    root = tree.getroot()
    if "Lang" in root.attrib:
        root.set("Lang", lang_code)

    attr_jobs, text_jobs, rich_jobs = collect_jobs(root)

    # Pre-compute text/rich scopes NOW, before the attr_jobs loop mutates
    # element attributes (Name/ID/Key).  _elem_id() reads those attributes, so
    # if attr_jobs runs first the scopes would no longer match what scan_xml
    # stored in the DB — causing silent cache misses for every element whose
    # parent has a translatable identifier attribute.
    attr_job_scopes = [
        f"attr:{attr_name}:{elem.tag}:{_elem_id(elem)}"
        for elem, attr_name, segments, is_trans in attr_jobs
    ]
    text_job_scopes = [
        (
            f"text:{elem.tag}"
            f":{parent.tag if parent is not None else 'root'}"
            f":{_elem_id(parent) or _elem_id(elem)}"
        )
        for elem, parent, src in text_jobs
    ]
    rich_job_scopes = [
        (
            f"block:{rich_elem.tag}"
            f":{parent_elem.tag if parent_elem is not None else 'root'}"
            f":{_elem_id(parent_elem)}"
        )
        for rich_elem, parent_elem in rich_jobs
    ]

    # ── Attributes ────────────────────────────────────────────────────────────
    for job, scope in zip(attr_jobs, attr_job_scopes):
        elem, attr_name, segments, is_trans = job
        for seg_idx, (seg, should_tr) in enumerate(zip(segments, is_trans)):
            if should_tr and seg.strip():
                sh = _source_hash(seg)
                t = db_cache.get((filename, scope, sh))
                if t is not None:
                    segments[seg_idx] = t
                    counters["applied"] += 1
                counters["total"] += 1
        elem.attrib[attr_name] = "~".join(segments)

    # ── Simple text elements ──────────────────────────────────────────────────
    for (elem, parent, src), scope in zip(text_jobs, text_job_scopes):
        sh = _source_hash(src)
        t = db_cache.get((filename, scope, sh))
        if t is not None:
            elem.text = t
            counters["applied"] += 1
        counters["total"] += 1

    # ── Rich text blocks ──────────────────────────────────────────────────────
    for (rich_elem, parent_elem), scope in zip(rich_jobs, rich_job_scopes):
        paragraphs, joined, global_map = _build_block_text(rich_elem)
        if paragraphs:
            sh = _source_hash(joined)
            t = db_cache.get((filename, scope, sh))
            counters["total"] += 1
            if t is not None:
                translated_parts = [p.strip() for p in t.split("\n\n") if p.strip()]
                for i, p_elem in enumerate(paragraphs):
                    if i < len(translated_parts):
                        _restore_block_paragraph(translated_parts[i], p_elem, global_map)
                counters["applied"] += 1

        # case/default atoms inside the rich block
        for child_elem, inline_ch, raw, atom_scope in collect_case_atoms(rich_elem):
            sh = _source_hash(raw)
            t = db_cache.get((filename, atom_scope, sh))
            counters["total"] += 1
            if t is not None:
                _restore_inline(t, child_elem, inline_ch)
                counters["applied"] += 1

    # Strip MARKER from rich elements' own .text
    for rich_elem, _ in rich_jobs:
        if rich_elem.text and MARKER in rich_elem.text:
            cleaned = rich_elem.text.replace(MARKER, "").strip()
            rich_elem.text = cleaned if cleaned else None

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    xml_body = ET.tostring(root, encoding="unicode")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write(xml_body)
        f.write("\n")

    return counters


def _output_filename(example_name: str, lang_code: str) -> str:
    base = example_name.replace(".example.xml", "")
    return f"{base}.{lang_code}.xml"


# ── Management command ────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Rebuild .es.xml files from DB translations."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--language", type=str, help="Language name (e.g. Spanish)")
        group.add_argument("--language-id", type=int, help="Language DB id")
        parser.add_argument(
            "--example-dir",
            type=str,
            help="Path to the game's ExampleLanguage folder "
                 "(default: streaming_assets_dir/Base/ExampleLanguage from config.toml)",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            help="Directory where .es.xml files will be written "
                 "(default: <project_root>/<mod_name>/languages/lang-<lang_code>/)",
        )

    def handle(self, *args, **options):
        languages = self._get_languages(options)

        for lang in languages:
            self.stdout.write(f"Exporting XML for: {lang}")

            # ── Resolve example_dir ───────────────────────────────────────────
            if options.get("example_dir"):
                example_dir = Path(options["example_dir"])
            else:
                # 1. env var (set by Docker compose)
                env_dir = os.environ.get("EXAMPLE_LANGUAGE_DIR")
                # 2. STREAMING_ASSETS_DIR from .env
                sa = settings.STREAMING_ASSETS_DIR
                if env_dir:
                    example_dir = Path(env_dir)
                elif sa:
                    example_dir = Path(sa) / "Base" / "ExampleLanguage"
                else:
                    raise CommandError(
                        "Cannot find ExampleLanguage dir. Set EXAMPLE_LANGUAGE_DIR "
                        "or STREAMING_ASSETS_DIR in .env, or pass --example-dir explicitly."
                    )

            if not example_dir.is_dir():
                raise CommandError(f"Example dir not found: {example_dir}")

            # ── Resolve output_dir ────────────────────────────────────────────
            if options.get("output_dir"):
                output_dir = Path(options["output_dir"])
            else:
                mod_name = settings.MOD_NAME or lang.name
                output_dir = (
                    Path(settings.BASE_DIR).parent / mod_name / "languages" / f"lang-{lang.lang_code}"
                )

            # ── Load DB cache ─────────────────────────────────────────────────
            self.stdout.write("  Loading translations from DB…")
            entries = (
                TranslationEntry.objects
                .filter(language=lang)
                .exclude(status=TranslationEntry.STATUS_FAILED)
                .select_related("source_file")
                .only("source_file__name", "scope", "source_hash", "translation")
            )
            db_cache = {
                (e.source_file.name, e.scope, e.source_hash): e.translation
                for e in entries
            }
            self.stdout.write(f"  {len(db_cache)} translations loaded.")

            # ── Process each .example.xml ─────────────────────────────────────
            example_files = sorted(example_dir.glob("*.example.xml"))
            if not example_files:
                self.stderr.write(f"  No .example.xml files found in {example_dir}")
                continue

            total_total = 0
            total_applied = 0

            for ex_path in example_files:
                out_name = _output_filename(ex_path.name, lang.lang_code)
                out_path = output_dir / out_name
                counts = _apply_translations(ex_path, out_path, db_cache, lang.lang_code)
                total_total += counts["total"]
                total_applied += counts["applied"]
                pct = (counts["applied"] / counts["total"] * 100) if counts["total"] else 0
                self.stdout.write(
                    f"    {out_name}: {counts['applied']}/{counts['total']} ({pct:.0f}%)"
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Done — {total_applied}/{total_total} strings applied "
                    f"across {len(example_files)} files → {output_dir}"
                )
            )

    def _get_languages(self, options):
        if options.get("language"):
            qs = Language.objects.filter(name=options["language"])
            if not qs.exists():
                raise CommandError(f"Language '{options['language']}' not found in DB.")
            return qs
        if options.get("language_id"):
            qs = Language.objects.filter(pk=options["language_id"])
            if not qs.exists():
                raise CommandError(f"No language with id={options['language_id']}.")
            return qs
        return Language.objects.all()
