# Generated by Django 3.2.6 on 2021-08-24 14:22

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('viewer', '0126_alter_archivemanageentry_options'),
    ]

    operations = [
        migrations.AlterField(
            model_name='archivemanageentry',
            name='mark_user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='archive_entry_mark', to=settings.AUTH_USER_MODEL),
        ),
    ]
