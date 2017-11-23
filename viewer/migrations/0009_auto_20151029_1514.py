# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0008_auto_20151029_1513'),
    ]

    operations = [
        migrations.AlterField(
            model_name='wantedgallery',
            name='announces',
            field=models.ManyToManyField(to='viewer.Announce', blank=True),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='artists',
            field=models.ManyToManyField(to='viewer.Artist', blank=True),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='possible_matches',
            field=models.ManyToManyField(to='viewer.Gallery', related_name='gallery_matches', blank=True, through='viewer.GalleryMatch'),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='unwanted_tags',
            field=models.ManyToManyField(to='viewer.Tag', related_name='unwanted_tags', blank=True),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='wanted_tags',
            field=models.ManyToManyField(to='viewer.Tag', blank=True),
        ),
    ]
