# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0012_gallery_public'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='wantedgallery',
            options={'ordering': ['-release_date'], 'verbose_name_plural': 'Wanted galleries'},
        ),
    ]
