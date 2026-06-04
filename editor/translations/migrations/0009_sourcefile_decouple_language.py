import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("translations", "0008_sourcefile_consolidate_data"),
    ]

    operations = [
        # 1. Drop the unique_together that includes 'language'
        migrations.AlterUniqueTogether(
            name="sourcefile",
            unique_together=set(),
        ),
        # 2. Remove the language FK
        migrations.RemoveField(
            model_name="sourcefile",
            name="language",
        ),
        # 3. Add a simple unique constraint on name
        migrations.AlterField(
            model_name="sourcefile",
            name="name",
            field=models.CharField(max_length=200, unique=True),
        ),
    ]
