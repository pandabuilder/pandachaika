# Generated by Django 4.1.5 on 2023-01-13 19:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0150_alter_eventlog_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='image',
            name='image_format',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='image',
            name='image_mode',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
