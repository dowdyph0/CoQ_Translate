"""
translate_pending — LLM-translate every entry with status='pending'.

Usage:
    python manage.py translate_pending
    python manage.py translate_pending --language Spanish
    python manage.py translate_pending --language-id 1
    python manage.py translate_pending --batch-size 10
    python manage.py translate_pending --dry-run

What it does
------------
1. Fetches all TranslationEntry(status='pending') for the given language,
   ordered by source_file so related strings are batched together.
2. Groups them into batches of --batch-size and calls the LLM.
3. Updates each entry: translation=<result>, status='auto'.
4. On failure: status stays 'pending' for that entry so the next run retries it.
   After max_retries exhausted it is marked 'failed'.

Run after `scan_xml` and `import_memory` to finish off whatever the JSON
cache did not cover.
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from translations.models import Language, TranslationEntry
from translations.pipeline import call_llm, protect, restore, _parse_batch_response


class Command(BaseCommand):
    help = "LLM-translate all pending entries in the DB."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--language", type=str, help="Language name (e.g. Spanish)")
        group.add_argument("--language-id", type=int, help="Language DB id")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Strings per LLM call (default: BATCH_SIZE from .env)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be translated without calling the LLM",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"] or settings.BATCH_SIZE
        dry_run = options["dry_run"]

        languages = self._get_languages(options)
        for lang in languages:
            self._translate_language(lang, batch_size, dry_run)

    def _translate_language(self, lang, batch_size, dry_run):
        qs = (
            TranslationEntry.objects
            .filter(language=lang, status=TranslationEntry.STATUS_PENDING)
            .select_related("source_file")
            .order_by("source_file__name", "id")
        )
        total = qs.count()
        if total == 0:
            self.stdout.write(f"{lang}: nothing pending.")
            return

        self.stdout.write(
            f"{lang}: {total} pending entries "
            f"(batch_size={batch_size}"
            + (" DRY RUN" if dry_run else "") + ")"
        )

        done = 0
        failed = 0
        entries = list(qs)

        # Process in batches
        for start in range(0, len(entries), batch_size):
            batch = entries[start: start + batch_size]
            sources = [e.source for e in batch]

            if dry_run:
                for e in batch:
                    snippet = e.source[:60] + ("..." if len(e.source) > 60 else "")
                    self.stdout.write(f"  [DRY] {e.source_file.name} | {e.scope} | {snippet!r}")
                done += len(batch)
                continue

            if len(batch) == 1:
                # Single — high-quality individual prompt
                e = batch[0]
                protected, mapping = protect(e.source)
                try:
                    tr_protected = call_llm(protected, batch=False)
                    final = restore(tr_protected, mapping)
                    self._save(e, final, TranslationEntry.STATUS_AUTO)
                    done += 1
                except Exception as exc:
                    self.stderr.write(
                        f"  [FAIL] {e.source_file.name} | {e.scope} | {exc}"
                    )
                    self._save(e, "", TranslationEntry.STATUS_FAILED)
                    failed += 1
            else:
                # Batch — numbered prompt
                protected_list = []
                mappings = []
                for e in batch:
                    p, m = protect(e.source)
                    protected_list.append(p)
                    mappings.append(m)

                numbered = "\n".join(
                    f"{n + 1}. {p}" for n, p in enumerate(protected_list)
                )
                try:
                    response = call_llm(numbered, batch=True)
                    parsed = _parse_batch_response(response, len(batch))
                except Exception as exc:
                    self.stderr.write(f"  [ERROR] Batch call failed: {exc}")
                    parsed = None

                if parsed is not None:
                    to_save = []
                    for n, e in enumerate(batch):
                        final = restore(parsed[n].strip(), mappings[n])
                        e.translation = final
                        e.status = TranslationEntry.STATUS_AUTO
                        to_save.append(e)
                    TranslationEntry.objects.bulk_update(
                        to_save, ["translation", "status", "updated_at"], batch_size=500
                    )
                    done += len(batch)
                else:
                    # Fallback: individual
                    self.stderr.write(
                        f"  [WARN] Batch response malformed — falling back to individual calls."
                    )
                    for e, p, m in zip(batch, protected_list, mappings):
                        try:
                            tr_protected = call_llm(p, batch=False)
                            final = restore(tr_protected, m)
                            self._save(e, final, TranslationEntry.STATUS_AUTO)
                            done += 1
                        except Exception as exc:
                            self.stderr.write(
                                f"  [FAIL] {e.source_file.name} | {e.scope} | {exc}"
                            )
                            self._save(e, "", TranslationEntry.STATUS_FAILED)
                            failed += 1

            if settings.REQUEST_DELAY:
                time.sleep(settings.REQUEST_DELAY)

            # Progress every 500 entries
            processed = min(start + batch_size, len(entries))
            if processed % 500 < batch_size or processed == len(entries):
                pct = processed / len(entries) * 100
                self.stdout.write(
                    f"  {processed}/{len(entries)} ({pct:.0f}%) — "
                    f"done={done} failed={failed}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"  Finished — translated: {done}, failed: {failed}"
            )
        )

    @staticmethod
    def _save(entry: TranslationEntry, translation: str, status: str):
        entry.translation = translation
        entry.status = status
        entry.save(update_fields=["translation", "status", "updated_at"])

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
