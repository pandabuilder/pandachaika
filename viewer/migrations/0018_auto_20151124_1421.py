# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0017_auto_20151124_1218'),
    ]

    operations = [
        migrations.AlterField(
            model_name='image',
            name='archive_position',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name='image',
            name='position',
            field=models.PositiveIntegerField(default=1),
        ),
    ]
