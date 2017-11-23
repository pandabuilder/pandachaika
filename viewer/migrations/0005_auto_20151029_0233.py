# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0004_auto_20151029_0228'),
    ]

    operations = [
        migrations.AlterField(
            model_name='announce',
            name='image_height',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='announce',
            name='image_width',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
