# Generated by Django 4.1.5 on 2023-01-27 03:12

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0155_remove_historicalarchive_original_filename'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='archive',
            index=models.Index(models.F('binned'), name='archive_binned_only'),
        ),
    ]