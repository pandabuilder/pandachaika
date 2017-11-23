# Panda Backup
__WARNING__: **This is alpha/development software. Expect rough edges**.

Backend+Frontend for viewing, matching and downloading hentai-manga galleries in .zip format.

Runs the [panda.chaika.moe](https://panda.chaika.moe) website, behind Nginx.

Overview
---------------------
Program is separated into two parts, one command line program that you can feed e-hentai.org/exhentai.org (panda) links, and it will download them using either torrent or archive/GP/Credits. The files will be kept on the filesystem as zip files. After downloading, all associated metadata will be obtained and stored on a database. Downloads using torrents can be handled by downloading to an external server, from which you can then download to the running server using FTPS.

This program also does local file matching, getting metadata for files you've already downloaded from different providers. This is based on the title of the files, or also on the first file on those archives. See matchers.py of each provider folder for more details.

The second part of this program is a Django-based application, that will run a server to serve this files via web browser to your clients. It has several options to search based on the metadata retrieved. This part also can call the command line program to trigger the download of new galleries.

It also includes a JSON API and a UserScript for panda, that allows you to queue galleries for download from the actual panda site.

Note: For now it's designed to generate and match .zip files, doesn't work with unpacked galleries.

Note 2: SQLite as a backend hasn't been tested in a while, and can give problems because of threading.

Requirements
---------------------

Know how to run a Python program and how to install packages.

Python 3.6 is the target version, won't work on Python 2. Must have been compiled with sqlite support (If you're using that database).

See requirements.txt.

You can configure the requirements, and remove the modules you aren't going to use (MySQL, rarfile, pushover, etc.)

How to run
---------------------

Clone/download.

Read defaults.ini for explanations on most configurable options.

Copy defaults.ini to settings.ini. Edit settings.ini (before running it!).

Install the requirements:

- One by one (to filter unneeded packages)
- pip install -r requirements.txt
- Install virtualenv and generate a virtual environment.

Run "python manage.py migrate", to create the database.

Run "python manage.py createsuperuser", to create the default admin user.

Run "python manage.py collectstatic", to collect the static files (to serve from CherryPy or external webserver).

Run "python manage.py compress", to compress js/css static files.

To use Elasticsearch, after you install it correctly, check settings.ini accordingly and run "python manage.py push-to-index -r -p".

Start the webserver with "python server.py".

The two command line programs are "foldercrawlerrun.py" and "crawlerrun.py".

If you want to run every request through a proxy, export the environment variables:
~~~~
$ export HTTP_PROXY="http://10.10.1.10:3128"
$ export HTTPS_PROXY="http://10.10.1.10:1080"
~~~~

TODO
---------------------

I don't know.
