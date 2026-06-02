"""
Migration 0003: add source_hash field to TranslationEntry and update unique constraint.

- Adds source_hash = CharField(max_length=12) — MD5[:12] of source text
- Populates source_hash for ALL existing rows (preserves reviewed translations)
- Drops old unique_together (language, source_file, scope)
- Adds new unique_together (language, source_file, scope, source_hash)

After this migration, multiple strings with the same (source_file, scope) but
different source text can coexist in the DB. This fixes the scope-collision bug
that caused distinct strings to overwrite each other when their parent element
had no identifying attribute (e.g. <part Name="Render"> inside hundreds of
different creature objects).
"""
import hashlib

from django.db import migrations, models


def populate_source_hash(apps, schema_editor):
    TranslationEntry = apps.get_model("translations", "TranslationEntry")
    batch = []
    for entry in TranslationEntry.objects.all().iterator(chunk_size=500):
        entry.source_hash = hashlib.md5(entry.source.encode("utf-8")).hexdigest()[:12]
        batch.append(entry)
        if len(batch) >= 500:
            TranslationEntry.objects.bulk_update(batch, ["source_hash"])
            batch = []
    if batch:
        TranslationEntry.objects.bulk_update(batch, ["source_hash"])


class Migration(migrations.Migration):

    dependencies = [
        ("translations", "0002_create_superuser"),
    ]

    operations = [
        # 1. Add source_hash column (nullable/blank initially so existing rows are valid)
        migrations.AddField(
            model_name="translationentry",
            name="source_hash",
            field=models.CharField(blank=True, max_length=12),
        ),
        # 2. Populate source_hash for all existing rows
        migrations.RunPython(populate_source_hash, migrations.RunPython.noop),
        # 3. Drop old unique constraint
        migrations.AlterUniqueTogether(
            name="translationentry",
            unique_together=set(),
        ),
        # 4. Add new unique constraint that includes source_hash
        migrations.AlterUniqueTogether(
            name="translationentry",
            unique_together={("language", "source_file", "scope", "source_hash")},
        ),
    ]
