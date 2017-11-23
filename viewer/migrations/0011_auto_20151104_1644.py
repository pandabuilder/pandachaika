# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0010_tweetpost'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='gallerymatch',
            options={'ordering': ['-match_accuracy'], 'verbose_name_plural': 'Gallery matches'},
        ),
    ]
