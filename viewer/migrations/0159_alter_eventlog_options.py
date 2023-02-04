# Generated by Django 4.1.5 on 2023-01-30 17:11

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0158_alter_eventlog_options'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='eventlog',
            options={'ordering': ['-create_date'], 'permissions': (('read_all_logs', 'Can view a general log from all users'), ('read_activity_logs', 'Can view a general log for all activity, no users')), 'verbose_name_plural': 'Event logs'},
        ),
    ]
