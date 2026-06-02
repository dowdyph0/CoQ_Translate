"""
import_failures — read translation_failures.json and mark matching entries as 'failed'.

Usage:
    python manage.py import_failures
    python manage.py import_failures --language Spanish
    python manage.py import_failures --language-id 1

translation_failures.json is a list of objects with the same shape as memory entries.
Entries already marked 'reviewed' are not touched.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from translations.models import Language, TranslationEntry, _source_hash


def resolve_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (Path(settings.BASE_DIR) / raw).resolve()


class Command(BaseCommand):
    help = "Mark failed entries from translation_failures.json."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--language", type=str, help="Language name (e.g. Spanish)")
        group.add_argument("--language-id", type=int, help="Language DB id")

    def handle(self, *args, **options):
        languages = self._get_languages(options)

        for lang in languages:
            self.stdout.write(f"Importing failures for: {lang}")

            if not lang.failures_path:
                self.stdout.write(self.style.WARNING("  No failures_path set, skipping."))
                continue

            path = resolve_path(lang.failures_path)
            if not path.exists():
                self.stdout.write(self.style.WARNING(f"  File not found: {path}, skipping."))
                continue

            with open(path, encoding="utf-8") as f:
                failures = json.load(f)

            if not failures:
                self.stdout.write("  No failures found in file.")
                continue

            marked = skipped = not_found = 0

            for item in failures:
                file_name = item.get("file", "")
                scope = item.get("scope", "")
                source = item.get("source", item.get("text", ""))  # support both key names

                qs = TranslationEntry.objects.filter(
                    language=lang,
                    source_file__name=file_name,
                    scope=scope,
                )
                if source:
                    qs = qs.filter(source_hash=_source_hash(source))

                entry = qs.first()
                if entry is None:
                    not_found += 1
                    continue

                if entry.status == TranslationEntry.STATUS_REVIEWED:
                    skipped += 1
                    continue

                entry.status = TranslationEntry.STATUS_FAILED
                entry.save(update_fields=["status", "updated_at"])
                marked += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Done — marked: {marked}, skipped (reviewed): {skipped}, not found: {not_found}"
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
