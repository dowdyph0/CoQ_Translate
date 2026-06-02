"""
Migration 0004 — switch source_hash from MD5[:12] to full MD5 (32 chars).

max_length was already 32 since 0003, so no schema change is needed for
SQLite (VARCHAR length is advisory). We only need to repopulate all rows
whose stored hash is 12 chars long.
"""

import hashlib

from django.db import migrations


def repopulate_hashes(apps, schema_editor):
    TranslationEntry = apps.get_model("translations", "TranslationEntry")
    # Only rows whose source_hash is exactly 12 chars need updating.
    qs = TranslationEntry.objects.filter(source__isnull=False).exclude(source="")
    chunk = []
    for entry in qs.only("id", "source", "source_hash").iterator(chunk_size=500):
        full_hash = hashlib.md5(entry.source.encode("utf-8")).hexdigest()
        if entry.source_hash != full_hash:
            entry.source_hash = full_hash
            chunk.append(entry)
        if len(chunk) >= 500:
            TranslationEntry.objects.bulk_update(chunk, ["source_hash"])
            chunk = []
    if chunk:
        TranslationEntry.objects.bulk_update(chunk, ["source_hash"])


class Migration(migrations.Migration):

    dependencies = [
        ("translations", "0003_translationentry_source_hash"),
    ]

    operations = [
        migrations.RunPython(repopulate_hashes, migrations.RunPython.noop),
    ]
