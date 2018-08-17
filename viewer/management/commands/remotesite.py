import socket
from ftplib import FTP_TLS
import json
import os
import ssl
import time
import requests

from django.core.management.base import BaseCommand
from django.conf import settings
from viewer.models import Archive, Gallery

crawler_settings = settings.CRAWLER_SETTINGS


def get_gid_path_association(site_page, api_key):

    archives_gid = list(Archive.objects.filter(gallery__hidden=True).values_list('gallery__gid', flat=True))
    data = {
        'operation': 'archive_request',
        'api_key': api_key,
        'args': archives_gid
    }

    headers = {'Content-Type': 'application/json; charset=utf-8'}
    r = requests.post(
        site_page,
        data=json.dumps(data),
        headers={**headers},
        timeout=25
    )
    response_data = {}
    try:
        response_data = r.json()
    except(ValueError, KeyError):
        pass

    return response_data


def send_urls_from_archives(site_page, api_key, reason):

    url_list = [x.get_link() for x in Archive.objects.filter(gallery__hidden=True)]

    data = {
        'operation': 'queue_archives',
        'api_key': api_key,
        'args': url_list
    }

    if reason:
        data['archive_reason'] = reason

    headers = {'Content-Type': 'application/json; charset=utf-8'}
    r = requests.post(
        site_page,
        data=json.dumps(data),
        headers={**headers},
        timeout=25
    )
    try:
        response_data = r.json()
        yield("Number of new galleries queued: {}".format(response_data['result']))
    except(ValueError, KeyError):
        yield("Error parsing the response: {}".format(r.text))


def send_urls_from_galleries(site_page, api_key):

    url_list = [x.get_link() for x in Gallery.objects.filter(hidden=True)]

    data = {
        'operation': 'queue_galleries',
        'api_key': api_key,
        'args': url_list
    }

    headers = {'Content-Type': 'application/json; charset=utf-8'}
    r = requests.post(
        site_page,
        data=json.dumps(data),
        headers={**headers},
        timeout=25
    )
    try:
        response_data = r.json()
        yield("Number of new galleries queued: {}".format(response_data['result']))
    except(ValueError, KeyError):
        yield("Error parsing the response: {}".format(r.text))


def send_urls_fakku(site_page, api_key):

    url_list = [x.get_link() for x in Gallery.objects.filter(provider__contains='fakku')]

    data = {
        'operation': 'links',
        'api_key': api_key,
        'args': url_list
    }

    headers = {'Content-Type': 'application/json; charset=utf-8'}
    r = requests.post(
        site_page,
        data=json.dumps(data),
        headers={**headers},
        timeout=25
    )
    try:
        response_data = r.json()
        yield("Number of new galleries queued: {}".format(response_data['result']))
    except(ValueError, KeyError):
        yield("Error parsing the response: {}".format(r.text))


class FTPHandler(object):

    def upload_archive_file(self, local_filename, remote_filename, target_dir):

        yield("Uploading {} to FTP in directory: {}, filename: {}".format(local_filename, target_dir, remote_filename))

        local_filesize = os.stat(local_filename).st_size
        self.upload_total = os.stat(local_filename).st_size
        self.upload_current = 0

        if self.settings.ftps['no_certificate_check']:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False
        else:
            context = ssl.create_default_context()
        ftps = FTP_TLS(
            host=self.settings.ftps['address'],
            user=self.settings.ftps['user'],
            passwd=self.settings.ftps['passwd'],
            context=context,
            source_address=self.settings.ftps[
                'source_address'],
            timeout=self.settings.timeout_timer
        )
        ftps.cwd(target_dir)
        ftps.encoding = 'utf8'
        ftps.prot_p()
        for line in ftps.mlsd(facts=["size"]):
            if(line[0] == remote_filename and
                    local_filesize == int(line[1]["size"])):
                yield("File exists and size is equal.")
                ftps.close()
                return
        with open(local_filename, "rb") as file:
            for retry_count in range(3):
                try:
                    ftps.storbinary(
                        'STOR %s' % remote_filename,
                        file,
                        callback=lambda data, args=self.print_method: self.print_progress(data, args)
                    )
                except (ConnectionResetError, socket.timeout, TimeoutError):
                    yield("Upload failed, retrying...")
                else:
                    break
        yield("\nFile uploaded.")
        ftps.close()

    def print_progress(self, data, print_method):
        self.upload_current += len(data)
        progress = self.upload_current / self.upload_total * 100
        print_method("Upload progress: {:05.2f}%".format(progress), ending='\r')

    def check_remote(self, all_archives, remote_site_api, api_key, remote_folder):

        remote_info = get_gid_path_association(remote_site_api, api_key)

        yield ("Checking {} archives versus {} remote files".format(all_archives.count(), len(remote_info['result'])))

        for cnt, remote_archive in enumerate(remote_info['result'], start=1):

            yield("Checking remote file {} of {}".format(cnt, len(remote_info['result'])))

            local_archives = Archive.objects.filter(
                gallery__gid=remote_archive['gid'],
            )

            if local_archives and local_archives.count() == 1:
                local_archive = local_archives.first()
                if not os.path.isfile(local_archive.zipped.path):
                    yield("Found file, {}, but it's not present in the filesystem, skipping.".format(local_archive.title))
                    continue
                yield("Found file, {}, size: {}".format(local_archive.title, local_archive.filesize))
                for message in self.upload_archive_file(
                        local_archive.zipped.path,
                        os.path.split(remote_archive['zipped'])[1],
                        os.path.join(remote_folder, str(remote_archive['id']))):
                    yield(message)
            else:
                yield("Not local match for {}".format(remote_archive['zipped']))

        yield("Remote upload finished.")

    def __init__(self, c_settings, print_method=print):
        self.settings = c_settings
        self.upload_current = 0
        self.upload_total = 0
        self.print_method = print_method


class Command(BaseCommand):
    help = 'Interact with the remote site.'

    def add_arguments(self, parser):
        parser.add_argument('-a', '--archives',
                            required=False,
                            action='store_true',
                            default=False,
                            help=(
                                'Send galleries marked as hidden to the remote site. '
                                'Must have been linked to an archive. '
                                'This will create a fake archive on the remote server '
                                'that will need to be uploaded with -u'))
        parser.add_argument('-ar', '--archive_reason',
                            required=False,
                            action='store',
                            default='',
                            help='Force a reason to the archives being sent.')
        parser.add_argument('-g', '--galleries',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Send galleries marked as hidden to the remote site.')
        parser.add_argument('-f', '--fakku',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Send gallery urls from FAKKU to the remote site.')
        parser.add_argument('-u', '--upload',
                            required=False,
                            action='store_true',
                            default=False,
                            help='Upload the archive files marked as missing hidden to the remote site.')

    def handle(self, *args, **options):
        start = time.perf_counter()
        if options['archives']:
            for message in send_urls_from_archives(crawler_settings.remote_site['api_url'], crawler_settings.remote_site['api_key'], options['archive_reason']):
                self.stdout.write(message)
        if options['galleries']:
            for message in send_urls_from_galleries(crawler_settings.remote_site['api_url'], crawler_settings.remote_site['api_key']):
                self.stdout.write(message)
        if options['fakku']:
            for message in send_urls_fakku(crawler_settings.remote_site['api_url'], crawler_settings.remote_site['api_key']):
                self.stdout.write(message)
        if options['upload']:
            all_archives = Archive.objects.filter_and_order_by_posted(
                gallery__hidden=True)

            ftp_handler = FTPHandler(crawler_settings, print_method=self.stdout.write)
            for message in ftp_handler.check_remote(
                    all_archives,
                    crawler_settings.remote_site['api_url'],
                    crawler_settings.remote_site['api_key'],
                    crawler_settings.remote_site['remote_folder']
            ):
                self.stdout.write(message)

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
