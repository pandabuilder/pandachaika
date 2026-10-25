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
export WEBSERVER_BACKEND=${WEBSERVER_BACKEND:-gunicorn}

SKIP_MIGRATIONS=${SKIP_MIGRATIONS:-false}
if [ "$SKIP_MIGRATIONS" != "true" ] && [ "$SKIP_MIGRATIONS" != "1" ]; then
    echo "Apply database migrations"
    gosu $USER:$GROUP python manage.py migrate
    echo "Adding provider data to database"
    gosu $USER:$GROUP python manage.py providers --scan-register
fi

if [ "$1" = "worker" ] || [ "$MODE" = "worker" ] || [ "$CONTAINER_ROLE" = "worker" ]; then
    if [ "$1" = "worker" ]; then
        shift
    fi
    echo "Starting PandaGallery worker process"
    exec gosu $USER:$GROUP python manage.py run_workers -c /config/ ${WORKER_ARGS} "$@"
fi

if [ "$#" -gt 0 ]; then
    echo "Executing custom command: $@"
    exec gosu $USER:$GROUP "$@"
fi

echo "Collecting static files"
gosu $USER:$GROUP python manage.py collectstatic --noinput
echo "Compressing static files"
gosu $USER:$GROUP python manage.py compress

START_WORKER=${START_WORKER:-auto}
WORKER_PID=""

if [ "$START_WORKER" = "auto" ]; then
    # Auto-detect if a separate dedicated worker container is reachable on the network
    if getent hosts worker >/dev/null 2>&1 || getent hosts pandachaika_worker >/dev/null 2>&1; then
        echo "Dedicated worker service detected on network, skipping internal background worker"
        START_WORKER="false"
    else
        echo "No dedicated worker service detected, running background worker in single-container mode"
        START_WORKER="true"
    fi
fi

if [ "$START_WORKER" = "true" ] || [ "$START_WORKER" = "1" ]; then
    echo "Starting worker process in background"
    gosu $USER:$GROUP python manage.py run_workers -c /config/ &
    WORKER_PID=$!
fi

echo "Starting server ($WEBSERVER_BACKEND)"
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