# Generated by Django 4.2.4 on 2023-08-07 23:59

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0168_archivesizeentry'),
    ]

    operations = [
        migrations.CreateModel(
            name='ArchiveFileEntry',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('archive_position', models.PositiveIntegerField(default=1)),
                ('position', models.PositiveIntegerField(default=1)),
                ('sha1', models.CharField(blank=True, max_length=50, null=True)),
                ('file_name', models.CharField(blank=True, default='', max_length=500)),
                ('file_type', models.CharField(blank=True, default='', max_length=40)),
                ('file_size', models.PositiveIntegerField(default=0)),
                ('description', models.CharField(blank=True, default='', max_length=500)),
                ('archive', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='viewer.archive')),
            ],
            options={
                'verbose_name_plural': 'Archive file entries',
                'ordering': ['-position'],
            },
        ),
        migrations.DeleteModel(
            name='ArchiveSizeEntry',
        ),
    ]
