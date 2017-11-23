# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0005_auto_20151029_0233'),
    ]

    operations = [
        migrations.CreateModel(
            name='GalleryMatch',
            fields=[
                ('id', models.AutoField(auto_created=True, verbose_name='ID', serialize=False, primary_key=True)),
                ('match_accuracy', models.FloatField(blank=True, default=0.0, null=True, verbose_name='Match accuracy')),
                ('gallery', models.ForeignKey(to='viewer.Gallery')),
            ],
            options={
                'verbose_name_plural': 'Gallery matches',
            },
        ),
        migrations.RemoveField(
            model_name='gallerymatches',
            name='gallery',
        ),
        migrations.RemoveField(
            model_name='gallerymatches',
            name='wanted_gallery',
        ),
        migrations.AlterField(
            model_name='wantedgallery',
            name='possible_matches',
            field=models.ManyToManyField(blank=True, default='', to='viewer.Gallery', related_name='gallery_matches', through='viewer.GalleryMatch'),
        ),
        migrations.DeleteModel(
            name='GalleryMatches',
        ),
        migrations.AddField(
            model_name='gallerymatch',
            name='wanted_gallery',
            field=models.ForeignKey(to='viewer.WantedGallery'),
        ),
    ]
