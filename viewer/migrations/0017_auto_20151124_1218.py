# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0016_auto_20151124_1215'),
    ]

    operations = [
        migrations.AlterField(
            model_name='image',
            name='image_height',
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name='image',
            name='image_width',
            field=models.PositiveIntegerField(null=True),
        ),
    ]
