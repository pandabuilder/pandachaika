# Generated by Django 3.2.5 on 2021-08-16 21:55

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0114_rename_mark_result_archivemanageentry_mark_extra'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='eventlog',
            options={'ordering': ['-create_date'], 'permissions': (('read_all_logs', 'Can view a general log from all users'), ('read_delete_logs', 'Can view delete logs for Archives')), 'verbose_name_plural': 'Event logs'},
        ),
    ]
