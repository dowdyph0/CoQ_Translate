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

import json
import time
import traceback

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from translations.models import Language, TranslationEntry
from translations.pipeline import call_llm, protect, restore, _parse_batch_response, _est_output_tokens, _CHARS_PER_TOKEN, _EXPANSION, _JSON_OVERHEAD


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
            .filter(language=lang, status__in=[
                TranslationEntry.STATUS_PENDING,
                TranslationEntry.STATUS_FAILED,
            ])
            .select_related("source_file")
            .order_by("source_file__name", "id")
        )
        total = qs.count()
        if total == 0:
            self.stdout.write(f"{lang}: nothing pending or failed.")
            return

        self.stdout.write(
            f"{lang}: {total} entries to translate (pending+failed) "
            f"(batch_size={batch_size}"
            + (" DRY RUN" if dry_run else "") + ")"
        )

        done = 0
        failed = 0
        processed = 0

        token_budget = int(settings.MAX_TOKENS * 0.85)

        # ── Stream entries in chunks, build and process batches on the fly ───
        # Never materialises more than `chunk_size` DB rows at once.
        def _iter_batches():
            current: list[tuple] = []
            current_est = _JSON_OVERHEAD
            for e in qs.iterator(chunk_size=200):
                p, m = protect(e.source)
                item_tok = int(len(p) / _CHARS_PER_TOKEN * _EXPANSION)
                flush = current and (
                    len(current) >= batch_size
                    or current_est + item_tok > token_budget
                )
                if flush:
                    yield current
                    current = []
                    current_est = _JSON_OVERHEAD
                current.append((e, p, m))
                current_est += item_tok
            if current:
                yield current

        for batch in _iter_batches():

            if dry_run:
                for e, p, m in batch:
                    snippet = e.source[:60] + ("..." if len(e.source) > 60 else "")
                    self.stdout.write(f"  [DRY] {e.source_file.name} | {e.scope} | {snippet!r}")
                done += len(batch)
                processed += len(batch)
                continue

            if len(batch) == 1:
                # Single — high-quality individual prompt
                e, protected, mapping = batch[0]
                est = int(len(protected) / _CHARS_PER_TOKEN * _EXPANSION)
                max_tok = min(settings.MAX_TOKENS, int(est * 1.2) + 32)
                self.stdout.write(f"  [SOLO est={est} max_tok={max_tok}]")
                try:
                    tr_protected = call_llm(protected, batch=False, max_tokens_override=max_tok)
                    final = restore(tr_protected, mapping)
                    self._save(e, final, TranslationEntry.STATUS_AUTO)
                    done += 1
                except Exception:
                    self.stderr.write(
                        f"  [FAIL] {e.source_file.name} | {e.scope}\n"
                        + traceback.format_exc()
                    )
                    self._save(e, "", TranslationEntry.STATUS_FAILED)
                    failed += 1
            else:
                # Batch — JSON array input, structured output
                protected_list = [t[1] for t in batch]
                mappings       = [t[2] for t in batch]
                entries_batch  = [t[0] for t in batch]

                est = _est_output_tokens(protected_list)
                max_tok = min(settings.MAX_TOKENS, int(est * 1.2) + 32)
                self.stdout.write(f"  [BATCH/{len(batch)} est={est} max_tok={max_tok}]")

                user_input = json.dumps(protected_list, ensure_ascii=False)
                try:
                    response = call_llm(user_input, batch=True, n_items=len(batch), max_tokens_override=max_tok)
                    parsed = _parse_batch_response(response, len(batch))
                except Exception:
                    self.stderr.write(f"  [ERROR] Batch call failed:\n" + traceback.format_exc())
                    parsed = None

                if parsed is not None:
                    to_save = []
                    for n, e in enumerate(entries_batch):
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
                    for e, p, m in batch:
                        est_s = int(len(p) / _CHARS_PER_TOKEN * _EXPANSION)
                        max_tok_s = min(settings.MAX_TOKENS, int(est_s * 1.2) + 32)
                        try:
                            tr_protected = call_llm(p, batch=False, max_tokens_override=max_tok_s)
                            final = restore(tr_protected, m)
                            self._save(e, final, TranslationEntry.STATUS_AUTO)
                            done += 1
                        except Exception:
                            self.stderr.write(
                                f"  [FAIL] {e.source_file.name} | {e.scope}\n"
                                + traceback.format_exc()
                            )
                            self._save(e, "", TranslationEntry.STATUS_FAILED)
                            failed += 1

            processed += len(batch)
            if settings.REQUEST_DELAY:
                time.sleep(settings.REQUEST_DELAY)

            # Progress every 500 entries
            if processed % 500 < len(batch) or processed == total:
                pct = processed / total * 100
                self.stdout.write(
                    f"  {processed}/{total} ({pct:.0f}%) — "
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
        # Default: use (or create) the language defined in settings
        lang, created = Language.objects.get_or_create(
            name=settings.TARGET_LANGUAGE,
            defaults={
                "lang_code": settings.LANG_CODE,
                "mod_name":  settings.MOD_NAME,
            },
        )
        if created:
            self.stdout.write(
                self.style.WARNING(
                    f"Created Language '{lang}' from settings."
                )
            )
        return Language.objects.filter(pk=lang.pk)
