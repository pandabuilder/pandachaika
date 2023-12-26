# Generated by Django 4.1.6 on 2023-02-11 04:09

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0161_image_original_height_image_original_width'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='archive',
            options={'permissions': (('publish_archive', 'Can publish available archives'), ('manage_archive', 'Can manage available archives'), ('mark_archive', 'Can mark available archives'), ('view_marks', 'Can view archive marks'), ('match_archive', 'Can match archives'), ('update_metadata', 'Can update metadata'), ('recalc_fileinfo', 'Can recalculate file info'), ('upload_with_metadata_archive', 'Can upload a file with an associated metadata source'), ('expand_archive', 'Can extract and reduce archives'), ('compare_archives', 'Can compare archives based on different algorithms'), ('recycle_archive', 'Can utilize the Archive Recycle Bin'), ('archive_internal_info', 'Can see selected internal Archive information'), ('mark_similar_archive', 'Can run the similar Archives process'), ('read_archive_change_log', 'Can read the Archive change log'), ('modify_archive_tools', 'Can use tools that modify the underlying file'))},
        ),
    ]
