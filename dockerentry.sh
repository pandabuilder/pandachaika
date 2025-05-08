#!/bin/bash

PUID=${PUID:-1000}
PGID=${PGID:-1000}

GROUP="appuser"
USER="appuser"

groupmod -o -g "$PGID" $GROUP
usermod -o -u "$PUID" $USER

chown -R $USER:$GROUP /config/
chown -R $USER:$GROUP /app/

export MEDIA_ROOT=/media/
export STATIC_ROOT=/static/
export PANDA_CONFIG_DIR=/config/
echo "Apply database migrations"
gosu $USER:$GROUP python manage.py migrate
echo "Collecting static files"
gosu $USER:$GROUP python manage.py collectstatic --noinput
echo "Compressing static files"
gosu $USER:$GROUP python manage.py compress
echo "Adding provider data to database"
gosu $USER:$GROUP python manage.py providers --scan-register

echo "Starting server"
exec gosu $USER:$GROUP python server.py -c /config/