"""
import_memory — parse translation_memory.json into the database.

Usage:
    python manage.py import_memory
    python manage.py import_memory --language Spanish
    python manage.py import_memory --language-id 1

Rules:
  - New entries are created with status='auto'.
  - Existing entries with status='reviewed' keep their current translation.
  - Existing entries with any other status get their translation updated.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from translations.models import Language, SourceFile, TranslationEntry, _source_hash
from translations.pipeline import _memory_path

CHUNK = 500  # rows per bulk operation


class Command(BaseCommand):
    help = "Import translation_memory.json into the database (bulk mode)."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--language", type=str, help="Language name (e.g. Spanish)")
        group.add_argument("--language-id", type=int, help="Language DB id")

    def handle(self, *args, **options):
        languages = self._get_languages(options)

        for lang in languages:
            self.stdout.write(f"Importing memory for: {lang}")
            path = _memory_path(lang)

            if not path.exists():
                self.stdout.write(f"  No memory file found at {path} — skipping.")
                continue

            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            items = data.get("entries", []) if isinstance(data, dict) else data

            # ── 1. Ensure all SourceFile rows exist (one bulk_create) ──────────
            file_names = {item["file"] for item in items}
            SourceFile.objects.bulk_create(
                [SourceFile(name=name) for name in file_names],
                ignore_conflicts=True,
            )

            # ── 2. Load only 'reviewed' keys to protect them ──────────────────
            reviewed_keys = set(
                TranslationEntry.objects
                .filter(language=lang, status=TranslationEntry.STATUS_REVIEWED)
                .values_list("source_file__name", "scope", "source_hash")
            )

            # ── 3. Build update list for entries that already exist in DB ────────
            # We never INSERT new rows here — scan_xml owns that responsibility.
            # We only UPDATE existing entries whose exact key (file, scope, hash)
            # matches the JSON, so no orphan rows are ever created.
            existing_keys = set(
                TranslationEntry.objects
                .filter(language=lang)
                .values_list("source_file__name", "scope", "source_hash")
            )

            json_exact: dict[tuple, tuple] = {}  # (file, scope, hash) → (source, translation)
            skipped = 0
            for item in items:
                file_name = item["file"]
                scope = item["scope"]
                source = item["source"]
                translation = item.get("translation", "")
                sh = _source_hash(source)
                key = (file_name, scope, sh)
                if key in reviewed_keys:
                    skipped += 1
                    continue
                if key in existing_keys:
                    json_exact[key] = (source, translation)

            # ── 4. Bulk-update only existing entries (no new inserts) ──────────
            exact_updated = 0
            if json_exact:
                # Load the actual ORM objects for matching keys
                from django.db.models import Q
                # Build a lookup: source_hash → list of items (handles rare hash collisions)
                hash_to_items: dict[str, list] = {}
                for (fname, scope, sh), (src, tr) in json_exact.items():
                    hash_to_items.setdefault(sh, []).append((fname, scope, src, tr))

                entries_to_update = (
                    TranslationEntry.objects
                    .filter(language=lang, source_hash__in=hash_to_items.keys())
                    .exclude(status=TranslationEntry.STATUS_REVIEWED)
                    .select_related("source_file")
                )
                to_bulk_update: list[TranslationEntry] = []
                for entry in entries_to_update:
                    key = (entry.source_file.name, entry.scope, entry.source_hash)
                    if key in json_exact:
                        _src, _tr = json_exact[key]
                        entry.source = _src
                        entry.translation = _tr
                        entry.status = TranslationEntry.STATUS_AUTO
                        to_bulk_update.append(entry)
                TranslationEntry.objects.bulk_update(
                    to_bulk_update,
                    ["source", "translation", "status"],
                    batch_size=CHUNK,
                )
                exact_updated = len(to_bulk_update)

            # ── 5. Fallback: fill pending entries matched only by source_hash ──
            # After scan_xml, entries exist with the correct scope but status=pending
            # and no translation.  The JSON may have a translation for the same
            # source text under a different scope (old scope formula).
            # We fill those in without touching the scope.
            json_by_hash: dict[str, str] = {}  # source_hash → translation
            for item in items:
                tr = item.get("translation", "")
                if tr:
                    sh = _source_hash(item["source"])
                    # Last one wins — all translations for the same source text
                    # should be identical so this is safe.
                    json_by_hash[sh] = tr

            pending_qs = (
                TranslationEntry.objects
                .filter(language=lang, status=TranslationEntry.STATUS_PENDING)
                .only("id", "source_hash", "translation", "status")
            )
            to_fill: list[TranslationEntry] = []
            for entry in pending_qs:
                tr = json_by_hash.get(entry.source_hash)
                if tr:
                    entry.translation = tr
                    entry.status = TranslationEntry.STATUS_AUTO
                    to_fill.append(entry)

            if to_fill:
                TranslationEntry.objects.bulk_update(
                    to_fill,
                    ["translation", "status"],
                    batch_size=CHUNK,
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Done — exact match updated: {exact_updated}, "
                    f"filled pending by hash: {len(to_fill)}, "
                    f"skipped (reviewed): {skipped}"
                )
            )

    def _get_languages(self, options):
        if options["language"]:
            qs = Language.objects.filter(name=options["language"])
            if not qs.exists():
                raise CommandError(f"Language '{options['language']}' not found in DB.")
            return qs
        if options["language_id"]:
            qs = Language.objects.filter(pk=options["language_id"])
            if not qs.exists():
                raise CommandError(f"Language id={options['language_id']} not found in DB.")
            return qs
        return Language.objects.all()
