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
echo "Adding provider data to database"
gosu $USER:$GROUP python manage.py providers --scan-register

if [ "$1" = "worker" ] || [ "$MODE" = "worker" ] || [ "$CONTAINER_ROLE" = "worker" ]; then
    echo "Starting PandaGallery worker process"
    exec gosu $USER:$GROUP python manage.py run_workers -c /config/
fi

echo "Collecting static files"
gosu $USER:$GROUP python manage.py collectstatic --noinput
echo "Compressing static files"
gosu $USER:$GROUP python manage.py compress

START_WORKER=${START_WORKER:-true}
WORKER_PID=""

if [ "$START_WORKER" = "true" ] || [ "$START_WORKER" = "1" ]; then
    echo "Starting worker process in background"
    gosu $USER:$GROUP python manage.py run_workers -c /config/ &
    WORKER_PID=$!
fi

echo "Starting server"
gosu $USER:$GROUP python server.py -c /config/ &
SERVER_PID=$!

shutdown() {
    echo "Shutting down processes..."
    if [ -n "$WORKER_PID" ]; then
        kill -TERM "$WORKER_PID" 2>/dev/null
    fi
    if [ -n "$SERVER_PID" ]; then
        kill -TERM "$SERVER_PID" 2>/dev/null
    fi
    wait
    exit 0
}

trap shutdown SIGTERM SIGINT

wait $SERVER_PID
EXIT_CODE=$?
if [ -n "$WORKER_PID" ]; then
    kill -TERM "$WORKER_PID" 2>/dev/null
fi
wait
exit $EXIT_CODE