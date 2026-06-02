"""
scan_xml — walk every .example.xml and sync the DB with what the game defines.

Usage:
    python manage.py scan_xml
    python manage.py scan_xml --language Spanish
    python manage.py scan_xml --language-id 1
    python manage.py scan_xml --example-dir /custom/ExampleLanguage

What it does
------------
1. Parses each .example.xml using the same collect_jobs() + scope formulas as
   export_xml — so scan and export always use an identical key space.
2. For every translatable string found:
   - If (language, source_file, scope, source_hash) is NOT in the DB → creates
     a new TranslationEntry with status='pending' and translation=''.
   - If it already exists → leaves it untouched (preserves auto/reviewed).
3. Detects OBSOLETE entries: rows in DB whose (scope, source_hash) no longer
   appear in any XML.  These are reported but NOT deleted (translations are
   preserved in case the game restores them later).

After scan_xml:
  - Run `import_memory` to fill in translations from the JSON cache.
  - Run `translate_pending` to LLM-translate whatever is still pending.
  - Run `export_xml` to write the final .es.xml files.
"""

import os
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from translations.models import Language, SourceFile, TranslationEntry, _source_hash
from translations.pipeline import MARKER, collect_jobs, collect_case_atoms, _elem_id, _build_block_text


def _collect_strings(example_path: Path) -> list[tuple[str, str]]:
    """
    Parse example_path and return a list of (scope, source) for every
    translatable string — using the canonical scope formulas.
    """
    try:
        tree = ET.parse(str(example_path))
    except ET.ParseError as exc:
        raise CommandError(f"Cannot parse {example_path.name}: {exc}")

    root = tree.getroot()
    attr_jobs, text_jobs, rich_jobs = collect_jobs(root)

    results: list[tuple[str, str]] = []

    # Attributes
    for job in attr_jobs:
        elem, attr_name, segments, is_trans = job
        scope = f"attr:{attr_name}:{elem.tag}:{_elem_id(elem)}"
        for seg, should_tr in zip(segments, is_trans):
            if should_tr and seg.strip():
                results.append((scope, seg))

    # Simple text elements
    for elem, parent, src in text_jobs:
        scope = (
            f"text:{elem.tag}"
            f":{parent.tag if parent is not None else 'root'}"
            f":{_elem_id(parent) or _elem_id(elem)}"
        )
        results.append((scope, src))

    # Rich text blocks
    for rich_elem, parent_elem in rich_jobs:
        scope = (
            f"block:{rich_elem.tag}"
            f":{parent_elem.tag if parent_elem is not None else 'root'}"
            f":{_elem_id(parent_elem)}"
        )
        _, joined, _ = _build_block_text(rich_elem)
        if joined:
            results.append((scope, joined))

        # Case atoms inside the rich block
        for _child, _inline_ch, raw, atom_scope in collect_case_atoms(rich_elem):
            results.append((atom_scope, raw))

    return results


class Command(BaseCommand):
    help = "Sync the DB with .example.xml files: add new pending entries, report obsoletes."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--language", type=str, help="Language name (e.g. Spanish)")
        group.add_argument("--language-id", type=int, help="Language DB id")
        parser.add_argument(
            "--example-dir",
            type=str,
            help="Path to the game's ExampleLanguage folder "
                 "(default: STREAMING_ASSETS_DIR/Base/ExampleLanguage from .env)",
        )

    def handle(self, *args, **options):
        languages = self._get_languages(options)

        # Resolve example_dir once
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

        example_files = sorted(example_dir.glob("*.example.xml"))
        if not example_files:
            raise CommandError(f"No .example.xml files found in {example_dir}")

        for lang in languages:
            self.stdout.write(f"Scanning for: {lang}")
            self._scan_language(lang, example_files)

    def _scan_language(self, lang, example_files):
        # ── Step 1: collect all (file, scope, source_hash) from XMLs ─────────
        # xml_strings maps source_file_name → list of (scope, source, source_hash)
        xml_strings: dict[str, list[tuple[str, str, str]]] = {}
        parse_errors = 0

        for ex_path in example_files:
            try:
                strings = _collect_strings(ex_path)
            except CommandError as e:
                self.stderr.write(f"  [ERROR] {e}")
                parse_errors += 1
                continue
            xml_strings[ex_path.name] = [
                (scope, src, _source_hash(src)) for scope, src in strings
            ]

        total_xml = sum(len(v) for v in xml_strings.values())
        self.stdout.write(
            f"  Found {total_xml} translatable strings across "
            f"{len(xml_strings)} files ({parse_errors} parse errors)"
        )

        # ── Step 2: ensure all SourceFile rows exist ──────────────────────────
        SourceFile.objects.bulk_create(
            [SourceFile(language=lang, name=name) for name in xml_strings],
            ignore_conflicts=True,
        )
        sf_map = {
            sf.name: sf
            for sf in SourceFile.objects.filter(language=lang, name__in=xml_strings.keys())
        }

        # ── Step 3: load existing DB keys ────────────────────────────────────
        existing_keys = set(
            TranslationEntry.objects
            .filter(language=lang)
            .values_list("source_file__name", "scope", "source_hash")
        )

        # ── Step 4: build list of new pending entries ─────────────────────────
        to_create: list[TranslationEntry] = []
        xml_keys: set[tuple[str, str, str]] = set()
        seen_new: set[tuple[str, str, str]] = set()  # dedup within this scan

        for file_name, strings in xml_strings.items():
            sf = sf_map[file_name]
            for scope, src, sh in strings:
                key = (file_name, scope, sh)
                xml_keys.add(key)
                if key not in existing_keys and key not in seen_new:
                    seen_new.add(key)
                    to_create.append(TranslationEntry(
                        language=lang,
                        source_file=sf,
                        scope=scope,
                        source=src,
                        source_hash=sh,
                        translation="",
                        status=TranslationEntry.STATUS_PENDING,
                    ))

        # ── Step 5: bulk-insert new pending entries ───────────────────────────
        with transaction.atomic():
            created = TranslationEntry.objects.bulk_create(
                to_create,
                ignore_conflicts=True,
                batch_size=500,
            )

        # ── Step 6: detect obsoletes ──────────────────────────────────────────
        obsolete_keys = existing_keys - xml_keys
        already_translated = len(existing_keys) - len(obsolete_keys)

        self.stdout.write(
            self.style.SUCCESS(
                f"  New pending   : {len(to_create)}\n"
                f"  Already in DB : {already_translated}\n"
                f"  Obsolete      : {len(obsolete_keys)} "
                f"(kept, not deleted)"
            )
        )

        if obsolete_keys:
            # Group by file for readability
            by_file: dict[str, int] = defaultdict(int)
            for fname, _scope, _sh in obsolete_keys:
                by_file[fname] += 1
            self.stdout.write("  Obsolete breakdown:")
            for fname, count in sorted(by_file.items()):
                self.stdout.write(f"    {fname}: {count}")

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
