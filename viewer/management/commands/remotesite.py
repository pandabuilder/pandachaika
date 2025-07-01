import socket
from collections import defaultdict
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


def get_gid_path_association(archives_to_check, site_page, user_token):

    archives_gid_provider = list(archives_to_check.values_list("gallery__gid", "gallery__provider"))
    data = {"operation": "archive_request", "args": archives_gid_provider}

    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer {}".format(user_token)}
    r = requests.post(site_page, data=json.dumps(data), headers=headers, timeout=25)
    response_data = {}
    try:
        response_data = r.json()
    except (ValueError, KeyError):
        pass

    return response_data


def send_urls_from_archive_list(site_page, user_token, reason, details, archive_ids):

    url_list = [x.get_link() for x in Archive.objects.filter(pk__in=archive_ids)]

    data = {"operation": "force_queue_archives", "args": url_list}

    if reason:
        data["archive_reason"] = reason
    if details:
        data["archive_details"] = details

    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer {}".format(user_token)}
    r = requests.post(site_page, data=json.dumps(data), headers=headers, timeout=25)
    try:
        response_data = r.json()
        yield "Number of new galleries queued: {}".format(response_data["result"])
    except (ValueError, KeyError):
        yield "Error parsing the response: {}".format(r.text)


def send_urls_from_archives(site_page, user_token, reason, details):

    url_list = [x.get_link() for x in Archive.objects.filter(gallery__hidden=True)]

    data = {"operation": "queue_archives", "args": url_list}

    if reason:
        data["archive_reason"] = reason
    if details:
        data["archive_details"] = details

    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer {}".format(user_token)}

    r = requests.post(site_page, data=json.dumps(data), headers=headers, timeout=25)
    try:
        response_data = r.json()
        yield "Number of new galleries queued: {}".format(response_data["result"])
    except (ValueError, KeyError):
        yield "Error parsing the response: {}".format(r.text)


def send_urls_from_gallery_query(site_page, user_token, reason, details, web_page, sessionid):

    galleries_request = requests.get(web_page, cookies={"sessionid": sessionid}, timeout=25)

    try:
        response_data = galleries_request.json()
    except (ValueError, KeyError):
        yield "Error parsing the response: {}".format(galleries_request.text)
        return

    gid_provider_list = [(x["gid"], x["provider"]) for x in response_data["galleries"]]

    gid_by_providers = defaultdict(list)
    for v, k in gid_provider_list:
        gid_by_providers[k].append(v)

    url_list = []
    used_archives = []

    for provider, gid_list in gid_by_providers.items():
        filtered_archives = Archive.objects.filter(gallery__gid__in=gid_list, gallery__provider=provider)
        url_list.extend([x.get_link() for x in filtered_archives])
        used_archives.extend([x.pk for x in filtered_archives])

    yield "Matched archives IDs with remote galleries: {}".format(used_archives)

    data = {"operation": "force_queue_archives", "args": url_list}

    if reason:
        data["archive_reason"] = reason
    if details:
        data["archive_details"] = details

    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer {}".format(user_token)}

    r = requests.post(site_page, data=json.dumps(data), headers=headers, timeout=25)
    try:
        response_data = r.json()
        yield "Number of galleries queued: {}".format(response_data["result"])

        with open("tmp_queue.json", "w") as f:
            json.dump(used_archives, f)

    except (ValueError, KeyError):
        yield "Error parsing the response: {}".format(r.text)


def send_urls_from_galleries(site_page, user_token):

    url_list = [x.get_link() for x in Gallery.objects.filter(hidden=True)]

    data = {"operation": "queue_galleries", "args": url_list}

    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer {}".format(user_token)}

    r = requests.post(site_page, data=json.dumps(data), headers=headers, timeout=25)
    try:
        response_data = r.json()
        yield "Number of new galleries queued: {}".format(response_data["result"])
    except (ValueError, KeyError):
        yield "Error parsing the response: {}".format(r.text)


def send_urls_fakku(site_page, user_token):

    url_list = [x.get_link() for x in Gallery.objects.filter(provider__contains="fakku")]

    data = {"operation": "links", "args": url_list}

    headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer {}".format(user_token)}

    r = requests.post(site_page, data=json.dumps(data), headers=headers, timeout=25)
    try:
        response_data = r.json()
        yield "Number of new galleries queued: {}".format(response_data["result"])
    except (ValueError, KeyError):
        yield "Error parsing the response: {}".format(r.text)


class FTPHandler(object):

    def __init__(self, c_settings, print_method=print):
        self.settings = c_settings
        self.upload_current = 0
        self.upload_total = 0
        self.print_method = print_method

    def upload_archive_file(self, local_filename, remote_filename, target_dir):

        yield "Uploading {} to FTP in directory: {}, filename: {}".format(local_filename, target_dir, remote_filename)

        local_filesize = os.stat(local_filename).st_size
        self.upload_total = os.stat(local_filename).st_size
        self.upload_current = 0

        if self.settings.ftps["no_certificate_check"]:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False
        else:
            context = ssl.create_default_context()
        ftps = FTP_TLS(
            host=self.settings.ftps["address"],
            user=self.settings.ftps["user"],
            passwd=self.settings.ftps["passwd"],
            context=context,
            source_address=self.settings.ftps["source_address"],
            timeout=self.settings.timeout_timer,
        )
        ftps.cwd(target_dir)
        ftps.encoding = "utf8"
        ftps.prot_p()
        for line in ftps.mlsd(facts=["size"]):
            if line[0] == remote_filename and local_filesize == int(line[1]["size"]):
                yield "File exists and size is equal."
                ftps.close()
                return
        with open(local_filename, "rb") as file:
            for retry_count in range(3):
                try:
                    ftps.storbinary(
                        "STOR %s" % remote_filename,
                        file,
                        callback=lambda data, args=self.print_method: self.print_progress(data, args),
                    )
                except (ConnectionResetError, socket.timeout, TimeoutError):
                    yield "Upload failed, retrying..."
                else:
                    break
        yield "\nFile uploaded."
        ftps.close()

    def print_progress(self, data, print_method):
        self.upload_current += len(data)
        progress = self.upload_current / self.upload_total * 100
        print_method("Upload progress: {:05.2f}%".format(progress), ending="\r")

    def check_remote(self, archives_to_check, remote_site_api, user_token, remote_folder):

        remote_info = get_gid_path_association(archives_to_check, remote_site_api, user_token)

        yield "Checking {} archives versus {} remote files".format(
            archives_to_check.count(), len(remote_info["result"])
        )

        for cnt, remote_archive in enumerate(remote_info["result"], start=1):

            yield "Checking remote file {} of {}".format(cnt, len(remote_info["result"]))

            local_archives = Archive.objects.filter(
                gallery__gid=remote_archive["gid"],
                gallery__provider=remote_archive["provider"],
            )

            if local_archives and local_archives.count() == 1:
                local_archive = local_archives.first()
                if not os.path.isfile(local_archive.zipped.path):
                    yield "Found file, {}, but it's not present in the filesystem, skipping.".format(
                        local_archive.title
                    )
                    continue
                yield "Found file, {}, size: {}".format(local_archive.title, local_archive.filesize)
                for message in self.upload_archive_file(
                    local_archive.zipped.path,
                    os.path.split(remote_archive["zipped"])[1],
                    os.path.join(remote_folder, str(remote_archive["id"])),
                ):
                    yield message
            else:
                yield "Not local match for {}".format(remote_archive["zipped"])

        yield "Remote upload finished."


class Command(BaseCommand):
    help = "Interact with the remote site."

    def add_arguments(self, parser):
        parser.add_argument(
            "-a",
            "--archives",
            required=False,
            action="store_true",
            default=False,
            help=(
                "Send galleries marked as hidden to the remote site. "
                "Must have been linked to an archive. "
                "This will create a fake archive on the remote server "
                "that will need to be uploaded with -u"
            ),
        )
        parser.add_argument(
            "-as",
            "--archive_specify",
            required=False,
            action="store",
            nargs="+",
            type=int,
            help="Specify list of archive ids to send.",
        )
        parser.add_argument(
            "-ar",
            "--archive_reason",
            required=False,
            action="store",
            default="",
            help="Force a reason to the archives being sent.",
        )
        parser.add_argument(
            "-ad",
            "--archive_details",
            required=False,
            action="store",
            default="",
            help="Set a detail to the archives being sent.",
        )
        parser.add_argument(
            "-g",
            "--galleries",
            required=False,
            action="store_true",
            default=False,
            help="Send galleries marked as hidden to the remote site.",
        )
        parser.add_argument(
            "-gfu",
            "--galleries_from_url",
            required=False,
            action="store",
            default="",
            help="Request galleries from a URL that returns JSON of gid, provider.",
        )
        parser.add_argument(
            "-f",
            "--fakku",
            required=False,
            action="store_true",
            default=False,
            help="Send gallery urls from FAKKU to the remote site.",
        )
        parser.add_argument(
            "-u",
            "--upload",
            required=False,
            action="store_true",
            default=False,
            help="Upload the archive files marked as missing hidden to the remote site.",
        )
        parser.add_argument(
            "-us",
            "--upload_specify",
            required=False,
            action="store",
            nargs="+",
            type=int,
            help="Specify list of archive ids to check and upload.",
        )
        parser.add_argument(
            "-uq",
            "--upload_queue",
            required=False,
            action="store_true",
            default=False,
            help="Upload archive files from the temp queue.",
        )

    def handle(self, *args, **options):
        start = time.perf_counter()
        if options["archive_specify"]:
            for message in send_urls_from_archive_list(
                crawler_settings.remote_site["api_url"],
                crawler_settings.remote_site["user_token"],
                options["archive_reason"],
                options["archive_details"],
                options["archive_specify"],
            ):
                self.stdout.write(message)
        if options["archives"]:
            for message in send_urls_from_archives(
                crawler_settings.remote_site["api_url"],
                crawler_settings.remote_site["user_token"],
                options["archive_reason"],
                options["archive_details"],
            ):
                self.stdout.write(message)
        if options["galleries_from_url"]:
            for message in send_urls_from_gallery_query(
                crawler_settings.remote_site["api_url"],
                crawler_settings.remote_site["user_token"],
                options["archive_reason"],
                options["archive_details"],
                options["galleries_from_url"],
                crawler_settings.remote_site["sessionid"],
            ):
                self.stdout.write(message)
        if options["galleries"]:
            for message in send_urls_from_galleries(
                crawler_settings.remote_site["api_url"], crawler_settings.remote_site["user_token"]
            ):
                self.stdout.write(message)
        if options["fakku"]:
            for message in send_urls_fakku(
                crawler_settings.remote_site["api_url"], crawler_settings.remote_site["user_token"]
            ):
                self.stdout.write(message)
        if options["upload"]:
            all_archives = Archive.objects.filter_and_order_by_posted(gallery__hidden=True)

            ftp_handler = FTPHandler(crawler_settings, print_method=self.stdout.write)
            for message in ftp_handler.check_remote(
                all_archives,
                crawler_settings.remote_site["api_url"],
                crawler_settings.remote_site["user_token"],
                crawler_settings.remote_site["remote_folder"],
            ):
                self.stdout.write(message)
        if options["upload_specify"]:
            specified_archives = Archive.objects.filter(id__in=options["upload_specify"])

            ftp_handler = FTPHandler(crawler_settings, print_method=self.stdout.write)
            for message in ftp_handler.check_remote(
                specified_archives,
                crawler_settings.remote_site["api_url"],
                crawler_settings.remote_site["user_token"],
                crawler_settings.remote_site["remote_folder"],
            ):
                self.stdout.write(message)

        if options["upload_queue"]:
            if not os.path.isfile("tmp_queue.json"):
                self.stdout.write(self.style.ERROR("Queue file: {} does not exist.".format("tmp_queue.json")))
                return
            with open("tmp_queue.json") as json_file:
                data = json.load(json_file)

            specified_archives = Archive.objects.filter(id__in=data)

            ftp_handler = FTPHandler(crawler_settings, print_method=self.stdout.write)
            for message in ftp_handler.check_remote(
                specified_archives,
                crawler_settings.remote_site["api_url"],
                crawler_settings.remote_site["user_token"],
                crawler_settings.remote_site["remote_folder"],
            ):
                self.stdout.write(message)

            os.remove("tmp_queue.json")

        end = time.perf_counter()

        self.stdout.write(
            self.style.SUCCESS(
                "Time taken (seconds, minutes): {0:.2f}, {1:.2f}".format(end - start, (end - start) / 60)
            )
        )
