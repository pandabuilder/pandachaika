# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0018_auto_20151124_1421'),
    ]

    operations = [
        migrations.CreateModel(
            name='FoundGallery',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('match_accuracy', models.FloatField(blank=True, null=True, verbose_name='Match accuracy', default=0.0)),
                ('source', models.CharField(blank=True, null=True, max_length=50, verbose_name='Source')),
                ('create_date', models.DateTimeField(auto_now_add=True)),
                ('gallery', models.ForeignKey(to='viewer.Gallery')),
                ('wanted_gallery', models.ForeignKey(to='viewer.WantedGallery')),
            ],
            options={
                'ordering': ['-create_date'],
                'verbose_name_plural': 'Found galleries',
            },
        ),
        migrations.AddField(
            model_name='wantedgallery',
            name='found_galleries',
            field=models.ManyToManyField(blank=True, through='viewer.FoundGallery', to='viewer.Gallery', related_name='found_galleries'),
        ),
    ]
