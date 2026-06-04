"""
Data migration: consolidate SourceFile rows before removing the language FK.

With a single language in DB (the common case), each source file name appears
at most once — so no merging is needed.  If somehow multiple languages share
the same file name, we pick the SourceFile with the lowest PK as the survivor
and re-point all TranslationEntry rows to it before the schema migration drops
the language column.
"""
from django.db import migrations


def consolidate_sourcefiles(apps, schema_editor):
    SourceFile = apps.get_model("translations", "SourceFile")
    TranslationEntry = apps.get_model("translations", "TranslationEntry")

    # Group SourceFile rows by name
    from collections import defaultdict
    by_name = defaultdict(list)
    for sf in SourceFile.objects.order_by("id"):
        by_name[sf.name].append(sf.id)

    for name, ids in by_name.items():
        if len(ids) <= 1:
            continue
        # Keep the first (lowest PK), redirect entries, delete duplicates
        survivor_id = ids[0]
        for dup_id in ids[1:]:
            TranslationEntry.objects.filter(source_file_id=dup_id).update(
                source_file_id=survivor_id
            )
            SourceFile.objects.filter(pk=dup_id).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("translations", "0007_remove_language_paths"),
    ]

    operations = [
        migrations.RunPython(consolidate_sourcefiles, migrations.RunPython.noop),
    ]
