# Generated by Django 5.1.2 on 2024-11-10 23:23

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('viewer', '0187_image_image_sha1'),
    ]

    operations = [
        migrations.CreateModel(
            name='Category',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='User friendly name', max_length=100)),
                ('slug', models.SlugField(unique=True)),
                ('create_date', models.DateTimeField(auto_now_add=True)),
                ('last_modified', models.DateTimeField(auto_now=True, null=True)),
            ],
        ),
        migrations.AddField(
            model_name='wantedgallery',
            name='categories',
            field=models.ManyToManyField(blank=True, to='viewer.category'),
        ),
    ]
