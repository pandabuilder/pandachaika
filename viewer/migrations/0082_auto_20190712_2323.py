# Generated by Django 2.2.3 on 2019-07-13 03:23

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0081_auto_20190712_2135'),
    ]

    operations = [
        migrations.AlterField(
            model_name='archivegroupentry',
            name='position',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='archivegroupentry',
            name='title',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
