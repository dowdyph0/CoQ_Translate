import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Language",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("lang_code", models.CharField(max_length=10)),
                ("mod_name", models.CharField(max_length=100)),
                ("memory_path", models.CharField(max_length=500)),
                ("failures_path", models.CharField(blank=True, max_length=500)),
            ],
            options={"verbose_name": "Language", "verbose_name_plural": "Languages"},
        ),
        migrations.CreateModel(
            name="SourceFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                (
                    "language",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_files",
                        to="translations.language",
                    ),
                ),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AlterUniqueTogether(
            name="sourcefile",
            unique_together={("language", "name")},
        ),
        migrations.CreateModel(
            name="TranslationEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scope", models.CharField(max_length=500)),
                ("source", models.TextField()),
                ("translation", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("auto", "Auto (LLM)"),
                            ("reviewed", "Reviewed"),
                            ("failed", "Failed"),
                            ("pending", "Pending"),
                        ],
                        default="auto",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "language",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entries",
                        to="translations.language",
                    ),
                ),
                (
                    "source_file",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entries",
                        to="translations.sourcefile",
                    ),
                ),
            ],
            options={
                "verbose_name": "Translation Entry",
                "verbose_name_plural": "Translation Entries",
                "ordering": ["source_file__name", "scope"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="translationentry",
            unique_together={("language", "source_file", "scope")},
        ),
    ]
