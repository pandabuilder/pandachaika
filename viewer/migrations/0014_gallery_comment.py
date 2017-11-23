# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0013_auto_20151113_1101'),
    ]

    operations = [
        migrations.AddField(
            model_name='gallery',
            name='comment',
            field=models.TextField(blank=True, default=''),
        ),
    ]
