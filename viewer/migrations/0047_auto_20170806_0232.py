# -*- coding: utf-8 -*-
# Generated by Django 1.11.3 on 2017-08-06 02:32
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0046_auto_20170806_0230'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='ProviderAttribute',
            new_name='Attribute',
        ),
    ]
