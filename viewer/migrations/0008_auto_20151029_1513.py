# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0007_auto_20151029_1128'),
    ]

    operations = [
        migrations.AlterField(
            model_name='wantedgallery',
            name='announces',
            field=models.ManyToManyField(blank=True, null=True, to='viewer.Announce'),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='artists',
            field=models.ManyToManyField(blank=True, null=True, to='viewer.Artist'),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='possible_matches',
            field=models.ManyToManyField(blank=True, null=True, through='viewer.GalleryMatch', related_name='gallery_matches', to='viewer.Gallery'),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='release_date',
            field=models.DateTimeField(verbose_name='Release date', blank=True, null=True, default=django.utils.timezone.now),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='unwanted_tags',
            field=models.ManyToManyField(blank=True, null=True, related_name='unwanted_tags', to='viewer.Tag'),
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='wanted_tags',
            field=models.ManyToManyField(blank=True, null=True, to='viewer.Tag'),
        ),
    ]
