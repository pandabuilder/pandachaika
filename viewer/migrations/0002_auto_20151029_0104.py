# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import viewer.models
import datetime


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Announce',
            fields=[
                ('id', models.AutoField(verbose_name='ID', auto_created=True, primary_key=True, serialize=False)),
                ('announce_date', models.DateTimeField(blank=True, verbose_name='Announce date', null=True)),
                ('release_date', models.DateTimeField(blank=True, verbose_name='Release date', null=True)),
                ('source', models.CharField(blank=True, null=True, default='', max_length=50)),
                ('comment', models.CharField(blank=True, null=True, default='', max_length=100)),
                ('image', models.ImageField(height_field='image_height', null=True, width_field='image_width', upload_to=viewer.models.upload_announce_handler, blank=True, max_length=500)),
                ('image_height', models.PositiveIntegerField()),
                ('image_width', models.PositiveIntegerField()),
                ('thumbnail', models.ImageField(blank=True, upload_to=viewer.models.upload_announce_thumb_handler, null=True, max_length=500)),
            ],
        ),
        migrations.CreateModel(
            name='Artist',
            fields=[
                ('id', models.AutoField(verbose_name='ID', auto_created=True, primary_key=True, serialize=False)),
                ('name', models.CharField(blank=True, null=True, default='', max_length=50)),
                ('name_jpn', models.CharField(blank=True, null=True, default='', max_length=50)),
            ],
        ),
        migrations.CreateModel(
            name='GalleryMatches',
            fields=[
                ('id', models.AutoField(verbose_name='ID', auto_created=True, primary_key=True, serialize=False)),
                ('match_accuracy', models.FloatField(blank=True, verbose_name='Match accuracy', null=True, default=0.0)),
                ('gallery', models.ForeignKey(to='viewer.Gallery')),
            ],
        ),
        migrations.CreateModel(
            name='WantedGallery',
            fields=[
                ('id', models.AutoField(verbose_name='ID', auto_created=True, primary_key=True, serialize=False)),
                ('title', models.CharField(blank=True, null=True, default='', max_length=500)),
                ('title_jpn', models.CharField(blank=True, null=True, default='', max_length=500)),
                ('book_type', models.CharField(blank=True, verbose_name='Book type', null=True, default='', max_length=20)),
                ('publisher', models.CharField(blank=True, verbose_name='Publisher', null=True, default='', max_length=20)),
                ('look_for_duration', models.DurationField(verbose_name='Look for duration', default=datetime.timedelta(30))),
                ('should_search', models.BooleanField(verbose_name='Should search', default=False)),
                ('found', models.BooleanField(verbose_name='Found', default=False)),
                ('date_found', models.DateTimeField(blank=True, verbose_name='Date found', null=True)),
                ('page_count', models.IntegerField(blank=True, verbose_name='Page count', null=True, default=0)),
                ('create_date', models.DateTimeField(auto_now_add=True)),
                ('last_modified', models.DateTimeField(auto_now=True, null=True)),
                ('announces', models.ManyToManyField(to='viewer.Announce', blank=True, default='')),
                ('artists', models.ManyToManyField(to='viewer.Artist', blank=True, default='')),
                ('cover_artist', models.ForeignKey(to='viewer.Artist', related_name='cover_artist')),
                ('matched_gallery', models.ForeignKey(to='viewer.Gallery')),
                ('possible_matches', models.ManyToManyField(to='viewer.Gallery', related_name='galleries_matches', blank=True, default='', through='viewer.GalleryMatches')),
                ('wanted_tags', models.ManyToManyField(to='viewer.Tag', blank=True, default='')),
            ],
            options={
                'verbose_name_plural': 'wanted_galleries',
            },
        ),
        migrations.AddField(
            model_name='gallerymatches',
            name='wanted_gallery',
            field=models.ForeignKey(to='viewer.WantedGallery'),
        ),
    ]
