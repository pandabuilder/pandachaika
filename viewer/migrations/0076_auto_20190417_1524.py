# Generated by Django 2.2 on 2019-04-17 19:24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0075_archivematches_match_type'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='gallery',
            constraint=models.UniqueConstraint(fields=('gid', 'provider'), name='unique_gallery'),
        ),
    ]
