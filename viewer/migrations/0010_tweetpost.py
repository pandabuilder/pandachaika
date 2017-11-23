# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0009_auto_20151029_1514'),
    ]

    operations = [
        migrations.CreateModel(
            name='TweetPost',
            fields=[
                ('id', models.AutoField(verbose_name='ID', auto_created=True, serialize=False, primary_key=True)),
                ('tweet_id', models.PositiveIntegerField(blank=True, null=True)),
                ('text', models.CharField(default='', max_length=200, null=True, blank=True)),
                ('user', models.CharField(default='', max_length=200, null=True, blank=True)),
                ('posted_date', models.DateTimeField(verbose_name='Posted date', blank=True, null=True, default=django.utils.timezone.now)),
                ('media_url', models.CharField(default='', max_length=200, null=True, blank=True)),
            ],
        ),
    ]
