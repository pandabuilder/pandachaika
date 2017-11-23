# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0002_auto_20151029_0104'),
    ]

    operations = [
        migrations.AddField(
            model_name='announce',
            name='type',
            field=models.CharField(null=True, max_length=50, blank=True, default=''),
        ),
    ]
