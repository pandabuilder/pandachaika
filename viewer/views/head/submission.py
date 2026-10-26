import json
import logging
import re
import uuid
import typing
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.core.paginator import EmptyPage, InvalidPage, Paginator
from django.db.models import Count, Q, QuerySet
from django.http import BadHeaderError, Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import linebreaks, urlize

from core.base.setup import Settings
from core.base.types import DataDict
from viewer.models import Gallery, GallerySubmitEntry, users_with_perm
from viewer.utils.actions import event_log
from viewer.utils.functions import send_mass_html_mail
from viewer.views.head.auth import render_error, render_message
from workers import client

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS


def url_submit(request: HttpRequest) -> HttpResponse:
    """Submit given URLs."""

    if not crawler_settings.urls.enable_public_submit:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_public_submit).")

    p = request.POST

    d = {}

    if p and "submit" in p:
        current_settings = Settings(load_from_config=crawler_settings.config)
        url_set = set()
        # create dictionary of properties for each url
        current_settings.back_up_downloaders = current_settings.downloaders.copy()
        current_settings.replace_metadata = False
        current_settings.config["allowed"]["replace_metadata"] = "no"
        for k, v in current_settings.config["downloaders"].items():
            current_settings.config["downloaders"][k] = str(-1)
            current_settings.downloaders[k] = -1
        current_settings.config["downloaders"]["panda_submit"] = str(1)
        current_settings.config["downloaders"]["nhentai_submit"] = str(1)
        current_settings.downloaders["panda_submit"] = 1
        current_settings.downloaders["nhentai_submit"] = 1
        for p_k, p_v in p.items():
            if isinstance(p_v, str) and p_k == "urls":
                url_list = p_v.split("\n")
                for item in url_list:
                    # Don't allow commands.
                    if not item.startswith("-"):
                        url_set.add(item.rstrip("\r"))
        urls = list(url_set)
        if not urls:
            messages.error(request, "Submission is empty.")
            return HttpResponseRedirect(reverse("viewer:url-submit"))

        reason = ""
        if "reason" in p and p["reason"] != "":
            reason = p["reason"]
            # Force limit string length (dl_type field max_length)
            # current_settings.gallery_reason = reason[:200]

        # As a security check, only finally set urls that pass the accepted_urls
        parsers = crawler_settings.provider_context.get_parsers(crawler_settings)

        url_messages = []
        admin_messages = []

        # Sometimes people submit messages here.
        found_valid_urls: list[str] = []
        url_provider_gallery_tuple = []

        for parser in parsers:
            if parser.id_from_url_implemented():
                urls_filtered = parser.filter_accepted_urls(urls)
                found_valid_urls.extend(urls_filtered)
                for url_filtered in urls_filtered:
                    gid = parser.id_from_url(url_filtered)
                    if not gid:
                        continue
                    gallery = Gallery.objects.filter(gid=gid, provider=parser.name).first()
                    url_provider_gallery_tuple.append((url_filtered, gallery, parser.name, gid))
                    if not gallery:
                        url_messages.append("{}: New URL, will be added to the submit queue".format(url_filtered))
                        messages.success(
                            request,
                            "URL {} was not in the backup and will be added to the submit queue.".format(url_filtered),
                        )
                        continue
                    if gallery.is_submitted():
                        if gallery.public:
                            messages.info(
                                request,
                                "URL {} already exists but a source file isn't available: {}.".format(
                                    url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                                ),
                            )
                            url_messages.append(
                                "{}: Already in submit queue but no Archive found, link: {}, reason: {}".format(
                                    url_filtered, request.build_absolute_uri(gallery.get_absolute_url()), gallery.reason
                                )
                            )
                        else:
                            messages.info(
                                request,
                                "URL {} already exists in the backup and it's being reviewed.".format(url_filtered),
                            )
                            url_messages.append(
                                "{}: Already in submit queue, link: {}, reason: {}".format(
                                    url_filtered, request.build_absolute_uri(gallery.get_absolute_url()), gallery.reason
                                )
                            )
                    elif gallery.is_deleted():
                        messages.info(request, "URL {} was deleted from the backup.".format(url_filtered))
                        url_messages.append(
                            "{}: Was marked as deleted: {}".format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                    elif gallery.is_denied():
                        messages.info(request, "URL {} was submitted but denied.".format(url_filtered))
                        url_messages.append(
                            "{}: Was submitted but denied: {}".format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                    elif gallery.public:
                        messages.info(
                            request,
                            "URL {} already exists in the backup: {}".format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                            ),
                        )
                        url_messages.append(
                            "{}: Already present, is public: {}".format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )
                    else:
                        messages.info(
                            request, "URL {} already exists in the backup and it's being reviewed.".format(url_filtered)
                        )
                        url_messages.append(
                            "{}: Already present, is not public: {}".format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                            )
                        )

        extra_text = [x for x in urls if x not in found_valid_urls]

        gallery_submit_entries: dict[str, GallerySubmitEntry] = {}
        gallery_submit_entries_by_id: dict[tuple[str, str], GallerySubmitEntry] = {}

        uuid_group = uuid.uuid4()

        for url_filtered, gallery, provider_name, gallery_id in url_provider_gallery_tuple:
            if not gallery:
                submit_entry = GallerySubmitEntry(
                    submit_url=url_filtered,
                    submit_reason=reason,
                    submit_extra="\n".join(extra_text),
                    submit_group=uuid_group,
                )
                submit_entry.save()
            else:
                submit_entry = GallerySubmitEntry(
                    gallery=gallery,
                    resolved_status=GallerySubmitEntry.RESOLVED_ALREADY_PRESENT,
                    submit_url=url_filtered,
                    submit_reason=reason,
                    submit_extra="\n".join(extra_text),
                    submit_group=uuid_group,
                )
                submit_entry.save()

            gallery_submit_entries[url_filtered] = submit_entry
            gallery_submit_entries_by_id[(gallery_id, provider_name)] = submit_entry

        no_commands_in_args_list: list[str] = list()
        for parser in parsers:
            if parser.id_from_url_implemented():
                no_commands_in_args_list.extend(parser.filter_accepted_urls(urls))

        current_settings.submit_group_uuid = str(uuid_group)

        client.enqueue(
            args=no_commands_in_args_list,
            task_type="web_crawler",
            override_options=current_settings,
            user=request.user,
            reason=reason,
        )

        if reason:
            admin_subject = "New submission, reason: {}".format(reason)
        else:
            admin_subject = "New submission, no reason given"

        if url_messages:
            admin_messages.append("URL details:\n{}".format("\n".join(url_messages)))

        if extra_text:
            admin_messages.append("Extra non-URL text:\n{}".format("\n".join(extra_text)))

        admin_messages.append(
            "\nYou can check the current submit queue in: {}".format(
                request.build_absolute_uri(reverse("viewer:submit-queue"))
            )
        )

        # if current_settings.mail_logging.enable:
        #     mail_admins(admin_subject, "\n".join(admin_messages), html_message=urlize("\n".join(admin_messages)))

        # Mail users
        users_to_mail = users_with_perm(
            "viewer",
            "approve_gallery",
            Q(email__isnull=False) | ~Q(email__exact=""),
            profile__notify_new_submissions=True,
        )

        mails = users_to_mail.values_list("email", flat=True)

        try:
            logger.info("New submission: sending emails to enabled users.")
            # (subject, message, from_email, recipient_list)
            datatuples = tuple(
                [
                    (
                        admin_subject,
                        "\n".join(admin_messages),
                        urlize(linebreaks("\n".join(admin_messages))),
                        crawler_settings.mail_logging.from_,
                        (mail,),
                    )
                    for mail in mails
                ]
            )
            send_mass_html_mail(datatuples, fail_silently=True)
        except BadHeaderError:
            logger.error("Failed sending emails: Invalid header found.")

        logger.info("{}\n{}".format(admin_subject, "\n".join(admin_messages)))

        return HttpResponseRedirect(reverse("viewer:url-submit"))
    elif p and "check" in p:
        submit_text = p.get("urls")

        url_dict = dict()
        if submit_text:
            for item in submit_text.split("\n"):
                # Don't allow commands.
                if not item.startswith("-"):
                    url_dict[item.rstrip("\r")] = 1

        url_list = list(url_dict.keys())
        parsers = crawler_settings.provider_context.get_parsers(crawler_settings)

        url_gallery_tuple = []

        for parser in parsers:
            if parser.id_from_url_implemented():
                urls_filtered = parser.filter_accepted_urls(url_list)
                for url_filtered in urls_filtered:
                    gid = parser.id_from_url(url_filtered)
                    if not gid:
                        continue
                    # We force public galleries only here.
                    found_galleries = Gallery.objects.filter(gid=gid, provider=parser.name).prefetch_related(
                        "archive_set", "alternative_sources"
                    )

                    url_gallery_tuple.append((url_filtered, found_galleries.first()))

        d.update({"results": url_gallery_tuple})

    return render(request, "viewer/url_submit.html", d)



