# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0015_auto_20151118_0020'),
    ]

    operations = [
        migrations.AddField(
            model_name='image',
            name='archive_position',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='image',
            name='extracted',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='image',
            name='sha1',
            field=models.CharField(blank=True, null=True, max_length=50),
        ),
    ]
