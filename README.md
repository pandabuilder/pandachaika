# Panda Backup
__WARNING__: **This is alpha/development software. Expect rough edges**.

Backend+Frontend for viewing, matching and downloading hentai-manga galleries in .zip format.

Runs the [panda.chaika.moe](https://panda.chaika.moe) website, behind Nginx.

Overview
---------------------
Program is separated into two parts, one command line program that you can feed g.e/ex links, and it will download them using either torrent or archive/GP/Credits. The files will be kept on the filesystem as zip files. After downloading, all associated metadata will be obtained and stored on a database. Downloads using torrents can be handled by downloading to an external server, from which you can then download to the running server using FTPS.

This program also does local file matching, getting metadata for files you've already downloaded from g.e/ex, nhentai, prettyhentai and google search for g.e/ex. This is based on the title of the files, or also on the first file on those archives.

The second part of this program is a Django-based application, that will run a server to serve this files via web browser to your clients. It has several options to search based on the metadata retrieved. This part also can call the command line program to trigger the download of new galleries.

It also includes a JSON API and a UserScript for g.e/ex, that allows you to queue galleries for download from the real place.

Note: For now it's designed to generate and match .zip files, doesn't work with uncompressed galleries.

Requirements
---------------------

Know how to run a Python program and how to install packages.

Python 3.5.0 is the target version, won't work on Python 2. Must have been compiled with sqlite support (If you're using that database).

See requirements.txt.

Must have requirements:

- requests
- Django
- django-compressor
- django-autocomplete-light
- Pillow
- django-debug-toolbar (Not necessary, improves debugging if something goes wrong).
- CherryPy (Not necessary if you use another webserver with fastcgi (apache, etc)).

Optional:

- rarfile is needed only if you're using auto .rar to .zip conversion, for torrent downloads that are sometimes in .rar. Also rarfile needs the rar executable to run and accessible from PATH.
- PyMySQL if you're using MySQL.
- psycopg2 if you're using PostgreSQL.
- transmissionrpc if you're using that client for torrent downloads.

How to run
---------------------

Clone/download.

Read defaults.ini for explanations on most configurable options.

Copy defaults.ini to settings.ini. Edit settings.ini (before running it!).

Install the requirements:

- One by one (to filter unneeded packages)
- pip install -r requirements.txt
- install virtualenv and generate a virtual environment.

Run "python manage.py migrate viewer", to create the database.

Run "python manage.py createsuperuser", to create the default admin user.

Run "python manage.py compress", to compress js/css static files.

Start the webserver with "python server.py".

The two command line programs are "foldercrawlerrun.py" and "crawlerrun.py".

TODO
---------------------

Improve the code quality (Heh).

Improve the web design (Not a web designer, but left most in a .css file so it can be improved).

Move more hardcoded stuff to the options file.

