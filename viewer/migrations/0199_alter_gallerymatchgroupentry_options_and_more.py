# Generated by Django 5.2 on 2025-05-02 19:31

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("viewer", "0198_alter_gallerymatchgroupentry_options"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="gallerymatchgroupentry",
            options={"ordering": ["gallery_position"], "verbose_name_plural": "Gallery match group entries"},
        ),
        migrations.AddConstraint(
            model_name="gallerymatchgroupentry",
            constraint=models.UniqueConstraint(
                fields=("gallery_match_group", "gallery_position"), name="unique_position_in_group"
            ),
        ),
    ]
