# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0003_announce_type'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='wantedgallery',
            options={'verbose_name_plural': 'Wanted galleries'},
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='cover_artist',
            field=models.ForeignKey(related_name='cover_artist', to='viewer.Artist', null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='matched_gallery',
            field=models.ForeignKey(to='viewer.Gallery', null=True, blank=True),
        ),
    ]
