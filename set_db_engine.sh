#!/bin/bash
cp default.yaml settings.yaml
sed -i -E "s/django_debug_mode: false/django_debug_mode: true/" settings.yaml
sed -i -E "s/db_engine: (.*)/db_engine: postgresql/" settings.yaml
sed -i -E "s/postgresql_name: (.*)/postgresql_name: $DB_NAME/" settings.yaml
sed -i -E "s/postgresql_user: (.*)/postgresql_user: $DB_USER/" settings.yaml
sed -i -E "s/postgresql_password: (.*)/postgresql_password: $DB_PASS/" settings.yaml
sed -i -E "s/postgresql_host: (.*)/postgresql_host: $DB_HOST/" settings.yaml
sed -i -E "s/postgresql_port: (.*)/postgresql_port: $DB_PORT/" settings.yaml
