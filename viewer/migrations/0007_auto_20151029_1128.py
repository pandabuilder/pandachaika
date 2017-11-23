# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0006_auto_20151029_0745'),
    ]

    operations = [
        migrations.AddField(
            model_name='wantedgallery',
            name='keep_searching',
            field=models.BooleanField(verbose_name='Keep searching', default=False),
        ),
        migrations.AddField(
            model_name='wantedgallery',
            name='release_date',
            field=models.DateTimeField(verbose_name='Release date', default=django.utils.timezone.now, blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wantedgallery',
            name='search_title',
            field=models.CharField(max_length=500, blank=True, null=True),
        ),
        migrations.AddField(
            model_name='wantedgallery',
            name='unwanted_tags',
            field=models.ManyToManyField(default='', blank=True, related_name='unwanted_tags', to='viewer.Tag'),
        ),
    ]
