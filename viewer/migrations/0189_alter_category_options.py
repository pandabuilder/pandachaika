# Generated by Django 5.1.2 on 2024-11-10 23:39

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0188_category_wantedgallery_categories'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='category',
            options={'ordering': ['name']},
        ),
    ]
