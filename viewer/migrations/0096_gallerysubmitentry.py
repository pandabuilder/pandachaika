# Generated by Django 3.1.7 on 2021-03-09 18:34

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0095_gallery_magazine'),
    ]

    operations = [
        migrations.CreateModel(
            name='GallerySubmitEntry',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('submit_url', models.TextField(blank=True, default='', null=True)),
                ('submit_reason', models.TextField(blank=True, default='', null=True)),
                ('submit_extra', models.TextField(blank=True, default='', null=True)),
                ('submit_result', models.TextField(blank=True, default='', null=True)),
                ('submit_date', models.DateTimeField(blank=True, default=django.utils.timezone.now)),
                ('create_date', models.DateTimeField(auto_now_add=True)),
                ('resolved_date', models.DateTimeField(blank=True, null=True)),
                ('resolved_status', models.SmallIntegerField(choices=[(1, 'Submitted'), (2, 'Approved'), (3, 'Denied'), (4, 'Already present')], db_index=True, default=1)),
                ('resolved_reason', models.CharField(blank=True, default='backup', max_length=200, null=True, verbose_name='Reason')),
                ('resolved_comment', models.TextField(blank=True, default='', null=True)),
                ('gallery', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='viewer.gallery')),
            ],
        ),
    ]
