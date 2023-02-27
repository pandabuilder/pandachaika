#!/bin/bash
cp default.yaml settings.yaml
sed -i -E "s/django_debug_mode: false/django_debug_mode: true/" settings.yaml
sed -i -E "s/db_engine: (.*)/db_engine: $DB_ENGINE/" settings.yaml
if [ "$DB_ENGINE" = 'mysql' ]
then
  sed -i -E "s/mysql_name: (.*)/mysql_name: $DB_NAME/" settings.yaml
  sed -i -E "s/mysql_user: (.*)/mysql_user: $DB_USER/" settings.yaml
  sed -i -E "s/mysql_password: (.*)/mysql_password: $DB_PASS/" settings.yaml
  sed -i -E "s/mysql_host: (.*)/mysql_host: $DB_HOST/" settings.yaml
  sed -i -E "s/mysql_port: (.*)/mysql_port: $DB_PORT/" settings.yaml
elif [ "$DB_ENGINE" = 'postgresql' ]
then
  sed -i -E "s/postgresql_name: (.*)/postgresql_name: $DB_NAME/" settings.yaml
  sed -i -E "s/postgresql_user: (.*)/postgresql_user: $DB_USER/" settings.yaml
  sed -i -E "s/postgresql_password: (.*)/postgresql_password: $DB_PASS/" settings.yaml
  sed -i -E "s/postgresql_host: (.*)/postgresql_host: $DB_HOST/" settings.yaml
  sed -i -E "s/postgresql_port: (.*)/postgresql_port: $DB_PORT/" settings.yaml
fi