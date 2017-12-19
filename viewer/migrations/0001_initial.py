# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import viewer.models
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Archive',
            fields=[
                ('id', models.AutoField(verbose_name='ID', primary_key=True, auto_created=True, serialize=False)),
                ('title', models.CharField(null=True, max_length=500, blank=True)),
                ('title_jpn', models.CharField(default='', null=True, max_length=500, blank=True)),
                ('zipped', models.FileField(verbose_name='File', max_length=500, upload_to=viewer.models.archive_path_handler)),
                ('crc32', models.CharField(verbose_name='CRC32', max_length=10, blank=True)),
                ('matchtype', models.CharField(default='', verbose_name='Match type', max_length=40, null=True, blank=True)),
                ('filesize', models.IntegerField(verbose_name='Size', null=True, blank=True)),
                ('filecount', models.IntegerField(verbose_name='Size', null=True, blank=True)),
                ('create_date', models.DateTimeField(auto_now_add=True)),
                ('last_modified', models.DateTimeField(auto_now=True, null=True)),
                ('source_type', models.CharField(default='panda', verbose_name='Source type', max_length=50, null=True, blank=True)),
                ('public', models.BooleanField(default=False)),
                ('thumbnail', models.ImageField(default='', max_length=500, upload_to=viewer.models.thumb_path_handler)),
            ],
        ),
        migrations.CreateModel(
            name='ArchiveMatches',
            fields=[
                ('id', models.AutoField(verbose_name='ID', primary_key=True, auto_created=True, serialize=False)),
                ('match_accuracy', models.FloatField(default=0.0, verbose_name='Match accuracy', null=True, blank=True)),
                ('archive', models.ForeignKey(to='viewer.Archive', on_delete=models.CASCADE)),
            ],
        ),
        migrations.CreateModel(
            name='Gallery',
            fields=[
                ('id', models.AutoField(verbose_name='ID', primary_key=True, auto_created=True, serialize=False)),
                ('gid', models.CharField(max_length=20)),
                ('token', models.CharField(null=True, max_length=50, blank=True)),
                ('title', models.CharField(default='', null=True, max_length=500, blank=True)),
                ('title_jpn', models.CharField(default='', null=True, max_length=500, blank=True)),
                ('category', models.CharField(default='', null=True, max_length=20, blank=True)),
                ('uploader', models.CharField(default='', null=True, max_length=50, blank=True)),
                ('posted', models.DateTimeField(verbose_name='Date posted', null=True, blank=True)),
                ('filecount', models.IntegerField(default=0, verbose_name='File count', null=True, blank=True)),
                ('filesize', models.IntegerField(default=0, verbose_name='Size', null=True, blank=True)),
                ('expunged', models.CharField(default='', null=True, max_length=10, blank=True)),
                ('rating', models.CharField(default='', null=True, max_length=10, blank=True)),
                ('hidden', models.BooleanField(default=False)),
                ('fjord', models.BooleanField(default=False)),
                ('dl_type', models.CharField(default='', max_length=40)),
                ('create_date', models.DateTimeField(auto_now_add=True)),
                ('last_modified', models.DateTimeField(auto_now=True, null=True)),
            ],
            options={
                'verbose_name_plural': 'galleries',
            },
        ),
        migrations.CreateModel(
            name='Image',
            fields=[
                ('id', models.AutoField(verbose_name='ID', primary_key=True, auto_created=True, serialize=False)),
                ('image', models.ImageField(null=True, upload_to=viewer.models.upload_imgpath_handler, blank=True, width_field='image_width', height_field='image_height', max_length=500)),
                ('image_height', models.PositiveIntegerField()),
                ('image_width', models.PositiveIntegerField()),
                ('thumbnail', models.ImageField(null=True, max_length=500, blank=True, upload_to=viewer.models.upload_thumbpath_handler)),
                ('position', models.PositiveIntegerField(default=0)),
                ('archive', models.ForeignKey(to='viewer.Archive', on_delete=models.CASCADE)),
            ],
            options={
                'ordering': ['position'],
            },
        ),
        migrations.CreateModel(
            name='Tag',
            fields=[
                ('id', models.AutoField(verbose_name='ID', primary_key=True, auto_created=True, serialize=False)),
                ('name', models.CharField(max_length=200)),
                ('scope', models.CharField(default='', max_length=200, blank=True)),
                ('source', models.CharField(default='web', verbose_name='Source', max_length=50, null=True, blank=True)),
                ('create_date', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='UserArchivePrefs',
            fields=[
                ('id', models.AutoField(verbose_name='ID', primary_key=True, auto_created=True, serialize=False)),
                ('favorite_group', models.IntegerField(default=1, verbose_name='Favorite Group')),
                ('archive', models.ForeignKey(to='viewer.Archive', on_delete=models.CASCADE)),
                ('user', models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE)),
            ],
        ),
        migrations.AddField(
            model_name='gallery',
            name='tags',
            field=models.ManyToManyField(default='', blank=True, to='viewer.Tag'),
        ),
        migrations.AddField(
            model_name='archivematches',
            name='gallery',
            field=models.ForeignKey(to='viewer.Gallery', on_delete=models.CASCADE),
        ),
        migrations.AddField(
            model_name='archive',
            name='custom_tags',
            field=models.ManyToManyField(default='', blank=True, to='viewer.Tag'),
        ),
        migrations.AddField(
            model_name='archive',
            name='gallery',
            field=models.ForeignKey(null=True, blank=True, to='viewer.Gallery', on_delete=models.SET_NULL),
        ),
        migrations.AddField(
            model_name='archive',
            name='possible_matches',
            field=models.ManyToManyField(default='', through='viewer.ArchiveMatches', to='viewer.Gallery', blank=True, related_name='possible_matches'),
        ),
        migrations.AddField(
            model_name='archive',
            name='user',
            field=models.ForeignKey(default=1, to=settings.AUTH_USER_MODEL, on_delete=models.SET_NULL),
        ),
    ]
