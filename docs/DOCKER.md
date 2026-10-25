# Docker Info

Get the repository
---------------------
```bash
git clone https://github.com/pandabuilder/pandachaika
cd pandachaika
```

Docker Compose (recommended)
---------------------
The recommended way to run the stack is using Docker Compose, using the provided [docker-compose.yml](../docker-compose.yml), and editing as needed. Depending on your infrastructure, you could turn off the database or the reverse proxy services. Copy the [sample.env](../sample.env) file to .env, and edit it (especially if exposing the server to the internet).

It is preferred to copy `docker-compose.yml` to a new file `docker-compose.local.yml`, for example, and edit it there.
Use [docker-compose.build.yml](../docker-compose.build.yml) instead for building images locally from source.

Remember to replace `DJANGO_SECRET_KEY`. The output from this command works:
```bash
python -c "import secrets; print(secrets.token_urlsafe())"
```

PUID and PGID should be of user/group that has enough permissions to write to the folders.

### Service Architecture

The stack supports both **single-container** and **multi-container** deployments:

- **Single-Container Mode (Default & Backward Compatible)**:
  `pandachaika` runs both the web server and the background workers (`run_workers`) in a single container. If you are running an older Docker Compose setup that only defines `pandachaika`, or using `docker run`, `START_WORKER=auto` automatically detects that no dedicated worker is on the network and launches the worker internally. No worker processes will be missed. You can also explicitly force this mode by setting `START_WORKER=true`.
- **Multi-Container Mode (Dedicated Workers)**:
  The Compose environment separates responsibilities across dedicated containers:
  - **`pandachaika`**: The Django web application and API (served via Gunicorn).
  - **`worker`**: The dedicated background worker process (`python manage.py run_workers`) handling downloads, crawlers, thumbnail generation, and scheduled tasks.
  - **`db`**: PostgreSQL database service with automated healthchecks.
  - **`nginx`**: Reverse proxy serving static assets and SSL termination.
  - **`elasticsearch`** *(Optional)*: Elasticsearch 8.x search cluster for full-text search and match indexing.

#### Independent Service Restarts

When running in multi-container mode, separating the web viewer and background workers allows you to restart either service independently without interrupting running operations:

```bash
# Restart only the web viewer (e.g. after template or view updates):
docker compose restart pandachaika

# Restart only background workers (e.g. to reload provider plugins or crawlers):
docker compose restart worker
```

#### Optional Elasticsearch Service

Elasticsearch is defined under the `elasticsearch` and `full` Compose profiles so it only starts when requested:

```bash
# Start stack including Elasticsearch:
docker compose --profile elasticsearch up -d

# Alternatively, set COMPOSE_PROFILES=elasticsearch in your .env file
```

### Configuration
For the full stack, you'll need at least 3 paths/mapped volumes:

- `/config`: Holds the configuration file `settings.yaml` and log files.
- `/media`: Holds the media files (zip files, images, uploads, etc.). 
- `/static`: Holds the static files for the backend, to be served by the Reverse Proxy.

These other paths are for the Nginx service (if used):

- `/var/www/media/`: Map it to the same folder used in the pandachaika container for media files.
- `/var/www/static/`: Map it to the same folder used in the pandachaika container for static files.
- `/etc/nginx/conf.d/`: Holds the nginx configuration (a generic one is [provided](../docker/nginx/conf.d/nginx.conf); edit it for certificates, server_name, etc.).
- `/etc/nginx/certs/`: Map a folder containing 2 files: a certificate (`nginx.crt`) and a private key (`nginx.key`).

You can create a self-signed certificate using:

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout ./certs/nginx.key -out ./certs/nginx.crt
```

### Run the stack (using published image)
```bash
docker compose up -d
```

### Run the stack (building from local changes)
```bash
docker compose -f docker-compose.build.yml up -d --build
```

### Post execution commands
```bash
# Create superuser:
docker compose exec -e PANDA_CONFIG_DIR=/config/ -e DJANGO_SUPERUSER_USERNAME=pandauser -e DJANGO_SUPERUSER_PASSWORD=SECRET_PASSWORD pandachaika python manage.py createsuperuser --email some@email.com --noinput

# Push search indexes to Elasticsearch (if running Elasticsearch):
docker compose exec -e PANDA_CONFIG_DIR=/config/ pandachaika python manage.py push-to-index -r -rg -rm
```

Build Image (If you know what you're doing)
---------------------
```bash
docker build -t pandachaika .
```

## Standalone Container (Development Only)

The following command will start the backend standalone, using the three specified folders (where `/config` must contain `settings.yaml`):

```bash
docker run -it -p 8090:8090 \
    --user 1000:1000 \
    -v ./config:/config/ \
    -v ./media:/media/ \
    -v ./static:/static/ \
    pandachaika
```
