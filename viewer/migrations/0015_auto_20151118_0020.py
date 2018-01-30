# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0014_gallery_comment'),
    ]

    operations = [
        migrations.AlterField(
            model_name='gallery',
            name='gid',
            field=models.CharField(max_length=200),
        ),
    ]
