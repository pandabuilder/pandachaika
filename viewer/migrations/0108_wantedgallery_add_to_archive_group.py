# Generated by Django 3.2.2 on 2021-05-31 02:24

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0107_alter_archive_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='wantedgallery',
            name='add_to_archive_group',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='viewer.archivegroup'),
        ),
    ]