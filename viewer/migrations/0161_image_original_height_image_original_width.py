# Generated by Django 4.1.6 on 2023-02-06 17:46

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0160_archivetag_archive_new_tags'),
    ]

    operations = [
        migrations.AddField(
            model_name='image',
            name='original_height',
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='image',
            name='original_width',
            field=models.PositiveIntegerField(null=True),
        ),
    ]
