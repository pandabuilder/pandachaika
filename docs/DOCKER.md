# Docker Info

Get the repository
---------------------
```bash
git clone https://github.com/pandabuilder/pandachaika
cd pandachaika
```

Docker Compose (recommended)
---------------------
The recommended way to run the stack is using Docker Compose, using the provided [docker-compose.yaml](../docker-compose.yaml), and editing as needed. Depending on your infrastructure, you could turn off the database or the reverse proxy services. Copy the [sample.env](../sample.env) file to .env, and edit it (specially if exposing the server to the internet)

It is preferred to copy docker-compose.yml to a new file docker-compose.local.yml, for example, and edit it there.
Use [docker-compose.build.yaml](../docker-compose.build.yaml) instead for building the image locally.

Remember to replace DJANGO_SECRET_KEY, the output from this command works:
```bash
python -c "import secrets; print(secrets.token_urlsafe())"
```

PUID and PGID should be of user/group that has enough permissions to write to the folders.

### Configuration
For the full stack, you'll need at least 3 paths/mapped volumes:

- /config: Holds the configuration file settings.yaml and log files.
- /media: Holds the media files (zip files, images, uploads, etc.). 
- /static: Holds the static files for the backend, to be served by the Reverse Proxy.

These other 4 are for the Nginx service (if used):

- /var/www/media/: Map it to the same folder used in the pandachaika container for media files
- /var/www/static/: Map it to the same folder used in the pandachaika container for static files
- /etc/nginx/conf.d/: Holds the nginx configuration (a generic one is [provided](../docker/nginx/conf.d/nginx.conf), you must edit it for certificate, server_name, etc.)
- /etc/nginx/certs/: Map a folder containing 2 files, a cert (nginx.crt) and a private key (nginx.key)

You can create a self-signed certificate using the command (change days to increase the expiration date):

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout ./certs/nginx.key -out ./certs/nginx.crt
```

### Run the stack (using published image)
```bash
docker compose -f docker-compose.local.yml up
```

### Run the stack (building from local changes)
```bash
docker compose -f docker-compose.local.yml up --build
```

### Post execution commands
```bash
docker compose exec -e PANDA_CONFIG_DIR=/config/ -e DJANGO_SUPERUSER_USERNAME=pandauser -e DJANGO_SUPERUSER_PASSWORD=SECRET_PASSWORD pandachaika python manage.py createsuperuser --email some@email.com --noinput # Creates the super user. Remember to replace user, password and emai with actual secure credentials
docker compose exec -e PANDA_CONFIG_DIR=/config/ pandachaika python manage.py push-to-index -r -rg -rm # Only needed if running the ElasticSearch integration
```

Build Image (If you know what you're doing)
---------------------
```bash
docker build -t pandachaika .
```

## Start Container

The following command will start the backend, using the three specified folders, from which /config should contain the settings.yaml file. You can create this file copying default.yaml and editing as needed.

This way of running will only run the backend (no database, no reverse proxy). Only recommended for development.

Note: it is mandatory to map a local directory/volume to the container /config path, since this path must contain the settings.yaml file.
This file configures the entire application (database, paths, etc.).

Should also at least map two extra directories to use as the media and static folder.

To ensure the folders are writeable, change the user to match the UID+GUID of the host that has write permissions for the config and media folders.
```bash
docker run -it -p 8090:8090 \
    --user 1000:1000 \
     -v ./config:/config/ \
     -v ./media:/media/ \
     -v ./static:/static/ \
     pandachaika
```

### Post execution commands
```bash
docker exec -d pandachaika -e DJANGO_SUPERUSER_USERNAME=pandauser -e DJANGO_SUPERUSER_PASSWORD=SECRET_PASSWORD pandachaika python manage.py createsuperuser --email some@email.com --noinput # Creates the super user. Remember to replace user, password and emai with actual secure credentials
docker exec -d pandachaika python manage.py push-to-index -r -rg -rm # Only needed if running the ElasticSearch integration
```