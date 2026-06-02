"""
export_memory — dump the database back to translation_memory.json.

Usage:
    python manage.py export_memory
    python manage.py export_memory --language Spanish
    python manage.py export_memory --language-id 1

The output format matches exactly what translate.py expects.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from translations.models import Language, TranslationEntry


def resolve_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (Path(settings.BASE_DIR) / raw).resolve()


class Command(BaseCommand):
    help = "Export DB translations back to translation_memory.json."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--language", type=str, help="Language name (e.g. Spanish)")
        group.add_argument("--language-id", type=int, help="Language DB id")

    def handle(self, *args, **options):
        languages = self._get_languages(options)

        for lang in languages:
            self.stdout.write(f"Exporting memory for: {lang}")
            path = resolve_path(lang.memory_path)

            entries = (
                TranslationEntry.objects.filter(language=lang)
                .select_related("source_file")
                .order_by("source_file__name", "scope")
            )

            data = {
                "language": lang.name,
                "lang_code": lang.lang_code,
                "entries": [
                    {
                        "file": e.source_file.name,
                        "scope": e.scope,
                        "source": e.source,
                        "translation": e.translation,
                    }
                    for e in entries
                ],
            }

            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Written {len(data['entries'])} entries → {path}"
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
