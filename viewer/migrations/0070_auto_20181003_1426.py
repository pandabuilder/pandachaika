# Generated by Django 2.1 on 2018-10-03 17:26

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0069_auto_20181003_1134'),
    ]

    operations = [
        migrations.AlterField(
            model_name='wantedgallery',
            name='search_title',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
