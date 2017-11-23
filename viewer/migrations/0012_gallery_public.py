# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0011_auto_20151104_1644'),
    ]

    operations = [
        migrations.AddField(
            model_name='gallery',
            name='public',
            field=models.BooleanField(default=False),
        ),
    ]
