# Generated by Django 3.2.13 on 2022-06-07 21:25

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('viewer', '0146_alter_galleryproviderdata_origin'),
    ]

    operations = [
        migrations.CreateModel(
            name='ItemProperties',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.PositiveIntegerField()),
                ('name', models.CharField(max_length=50)),
                ('tag', models.SlugField()),
                ('value', models.CharField(max_length=100)),
                ('content_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype')),
            ],
        ),
        migrations.AddIndex(
            model_name='itemproperties',
            index=models.Index(fields=['content_type', 'object_id'], name='viewer_item_content_8474cd_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='itemproperties',
            unique_together={('content_type', 'object_id', 'name')},
        ),
    ]
