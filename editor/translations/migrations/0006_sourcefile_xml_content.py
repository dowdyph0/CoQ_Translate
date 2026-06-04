from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('translations', '0005_alter_translationentry_source_hash'),
    ]

    operations = [
        migrations.AddField(
            model_name='sourcefile',
            name='xml_content',
            field=models.TextField(blank=True, default=''),
        ),
    ]
