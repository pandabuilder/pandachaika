# Generated by Django 5.0.6 on 2024-06-12 18:35

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0184_alter_gallery_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='monitoredlink',
            name='limited_wanted_galleries',
            field=models.ManyToManyField(blank=True, to='viewer.wantedgallery'),
        ),
        migrations.AddField(
            model_name='monitoredlink',
            name='use_limited_wanted_galleries',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='wantedgallery',
            name='restricted_to_links',
            field=models.BooleanField(blank=True, default=False, verbose_name='Restricted to MonitoredLinks'),
        ),
    ]