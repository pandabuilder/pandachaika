# Generated by Django 5.0.4 on 2024-04-12 01:26

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0177_alter_gallery_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='gallery',
            name='disowned',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='historicalgallery',
            name='disowned',
            field=models.BooleanField(default=False),
        ),
    ]
