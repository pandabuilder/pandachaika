# -*- coding: utf-8 -*-
# Generated by Django 1.11.3 on 2017-07-29 05:01
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0042_auto_20170729_0448'),
    ]

    operations = [
        migrations.RenameField(
            model_name='wantedgallery',
            old_name='wanted_page_count_higher',
            new_name='wanted_page_count_upper',
        ),
    ]
