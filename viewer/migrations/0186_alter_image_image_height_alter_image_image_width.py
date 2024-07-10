# Generated by Django 5.0.6 on 2024-06-13 03:17

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0185_monitoredlink_limited_wanted_galleries_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='image',
            name='image_height',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='image',
            name='image_width',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
