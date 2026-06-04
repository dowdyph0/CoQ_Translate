from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("translations", "0006_sourcefile_xml_content"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="language",
            name="memory_path",
        ),
        migrations.RemoveField(
            model_name="language",
            name="failures_path",
        ),
    ]
