from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("translations", "0009_sourcefile_decouple_language"),
    ]

    operations = [
        migrations.AlterField(
            model_name="translationentry",
            name="scope",
            field=models.CharField(max_length=2000),
        ),
    ]
