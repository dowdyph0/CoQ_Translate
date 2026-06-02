"""
Data migration: create a default superuser (admin / admin) if none exists.
Change the password after first login.
"""

from django.db import migrations


def create_superuser(apps, schema_editor):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    if not User.objects.filter(is_superuser=True).exists():
        user = User.objects.create(
            username="admin",
            email="",
            is_superuser=True,
            is_staff=True,
        )
        user.set_password("admin")
        user.save()


class Migration(migrations.Migration):

    dependencies = [
        ("translations", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_superuser, migrations.RunPython.noop),
    ]
