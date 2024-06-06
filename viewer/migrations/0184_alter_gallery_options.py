# Generated by Django 5.0.4 on 2024-04-26 22:17

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0183_downloadevent_gallery'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='gallery',
            options={'permissions': (('publish_gallery', 'Can publish available galleries'), ('private_gallery', 'Can set private available galleries'), ('download_gallery', 'Can download present galleries'), ('mark_delete_gallery', 'Can mark galleries as deleted'), ('add_deleted_gallery', 'Can add galleries as deleted'), ('manage_missing_archives', 'Can manage missing archives'), ('view_submitted_gallery', 'Can view submitted galleries'), ('approve_gallery', 'Can approve submitted galleries'), ('wanted_gallery_found', 'Can be notified of new wanted gallery matches'), ('crawler_adder', 'Can add links to the crawler with more options'), ('download_history', 'Can check the download history'), ('read_gallery_change_log', 'Can read the Gallery change log'), ('manage_gallery', 'Can manage available galleries')), 'verbose_name_plural': 'galleries'},
        ),
    ]
