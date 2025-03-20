#!/bin/bash

export PANDA_CONFIG_DIR=/config/
echo "Apply database migrations"
python manage.py migrate
echo "Collecting static files"
python manage.py collectstatic --noinput
echo "Compressing static files"
python manage.py compress
echo "Adding provider data to database"
python manage.py providers --scan-register

echo "Starting server"
python server.py -c /config/