# Generated by Django 4.2.1 on 2023-05-30 14:31

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0162_alter_archive_options'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='tag',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='tag',
            constraint=models.UniqueConstraint(fields=('scope', 'name'), name='unique_scope_name'),
        ),
    ]
