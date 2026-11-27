import json
import logging
import re
import uuid
from collections import defaultdict
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.db.models import Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import linebreaks, urlize

from core.base.setup import Settings
from viewer.models import Archive, Gallery, GallerySubmitEntry, users_with_perm
from viewer.views.head.auth import render_error
from workers import client

logger = logging.getLogger(__name__)
crawler_settings = settings.CRAWLER_SETTINGS

ANONYMOUS_MAX_CHECK_URLS = 100
AUTHENTICATED_MAX_CHECK_URLS = 300
SUBMIT_FEEDBACK_SESSION_KEY = "url_submit_feedback"

_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def _is_gallery_failed(gallery: Gallery) -> bool:
    return "failed" in (gallery.dl_type or "").lower()


def _is_archive_public_available(archive: Archive) -> bool:
    return bool(archive.public and not archive.binned and not archive.file_deleted)


def _is_archive_active(archive: Archive) -> bool:
    return bool(not archive.binned and not archive.file_deleted)


def parse_submit_lines(raw_text: str) -> list[str]:
    """Split textarea input into non-empty stripped lines while preserving order."""
    if not raw_text:
        return []
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if line:
            lines.append(line)
    return lines


def _collect_gallery_archives(gallery: Gallery) -> list[tuple[Archive, str]]:
    """Return deduplicated (archive, relation) pairs from prefetched main and alt relations."""
    main_archives = getattr(gallery, "visible_main_archives", None)
    if main_archives is None:
        main_archives = list(gallery.archive_set.all())
    alt_archives = getattr(gallery, "visible_alt_archives", None)
    if alt_archives is None:
        alt_archives = list(gallery.alternative_sources.all())

    combined: list[tuple[Archive, str]] = []
    seen_pks: set[int] = set()
    for archive in main_archives:
        if archive.pk not in seen_pks:
            seen_pks.add(archive.pk)
            combined.append((archive, "main"))
    for archive in alt_archives:
        if archive.pk not in seen_pks:
            seen_pks.add(archive.pk)
            combined.append((archive, "alt"))
    return combined


def _serialize_archive(archive: Archive, relation: str, *, user_is_authenticated: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": archive.pk,
        "title": archive.best_title,
        "url": reverse("viewer:archive", args=[archive.pk]),
        "thumbnail_url": archive.thumbnail.url if archive.thumbnail.name else "",
        "relation": relation,
        "public_date": archive.public_date.strftime("%Y-%m-%d") if archive.public_date else "",
        "filecount": archive.filecount or 0,
        "filesize": archive.filesize or 0,
    }
    if user_is_authenticated:
        data["public"] = bool(archive.public)
        data["recycled"] = bool(archive.is_recycled())
        data["file_deleted"] = bool(archive.is_file_deleted())
    return data


def _serialize_gallery(gallery: Gallery, *, user_is_authenticated: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": gallery.pk,
        "title": gallery.best_title,
        "url": reverse("viewer:gallery", args=[gallery.pk]),
        "thumbnail_url": gallery.thumbnail.url if gallery.thumbnail.name else "",
        "posted": gallery.posted.strftime("%Y-%m-%d") if gallery.posted else "",
        "filecount": gallery.filecount or 0,
        "filesize": gallery.filesize or 0,
        "category": gallery.category or "",
        "provider": gallery.provider or "",
    }
    if user_is_authenticated:
        data["public"] = bool(gallery.public)
        data["status_display"] = gallery.get_status_display()
        data["failed"] = _is_gallery_failed(gallery)
        data["is_submitted"] = bool(gallery.is_submitted())
    return data


def inspect_submitted_urls(
    raw_lines: list[str],
    *,
    user_is_authenticated: bool,
) -> dict[str, Any]:
    """Inspect a list of submitted lines against parsers and the database.

    Privacy invariant for anonymous callers (``user_is_authenticated=False``):
    - Only queries ``Gallery`` rows with ``public=True`` and ``status=NORMAL``.
    - Only queries ``Archive`` rows with ``public=True, binned=False, file_deleted=False``.
    - Never exposes non-public galleries, non-public archives, deleted/denied status,
      private archive existence, or internal ``dl_type`` / ``failed`` details.
    """
    parsers = [
        parser
        for parser in crawler_settings.provider_context.get_parsers(crawler_settings)
        if parser.id_from_url_implemented()
    ]

    parsed_entries: list[dict[str, Any]] = []
    seen_exact: set[str] = set()
    seen_provider_gid: dict[tuple[str, str], str] = {}
    lookup_by_provider: dict[str, set[str]] = defaultdict(set)

    for idx, line in enumerate(raw_lines):
        if line.startswith("-"):
            parsed_entries.append(
                {
                    "index": idx,
                    "input": line,
                    "kind": "invalid",
                    "provider": "",
                    "gid": "",
                    "duplicate_of": "",
                }
            )
            continue

        if line in seen_exact:
            parsed_entries.append(
                {
                    "index": idx,
                    "input": line,
                    "kind": "duplicate",
                    "provider": "",
                    "gid": "",
                    "duplicate_of": line,
                }
            )
            continue
        seen_exact.add(line)

        matched_provider = ""
        matched_gid = ""
        for parser in parsers:
            if parser.filter_accepted_urls([line]):
                gid = parser.id_from_url(line)
                if gid:
                    matched_provider = parser.name
                    matched_gid = gid
                    break

        if not matched_provider:
            kind = "unsupported" if _URL_SCHEME_RE.match(line) else "note"
            parsed_entries.append(
                {
                    "index": idx,
                    "input": line,
                    "kind": kind,
                    "provider": "",
                    "gid": "",
                    "duplicate_of": "",
                }
            )
            continue

        key = (matched_provider, matched_gid)
        if key in seen_provider_gid:
            parsed_entries.append(
                {
                    "index": idx,
                    "input": line,
                    "kind": "duplicate",
                    "provider": matched_provider,
                    "gid": matched_gid,
                    "duplicate_of": seen_provider_gid[key],
                }
            )
            continue

        seen_provider_gid[key] = line
        lookup_by_provider[matched_provider].add(matched_gid)
        parsed_entries.append(
            {
                "index": idx,
                "input": line,
                "kind": "url",
                "provider": matched_provider,
                "gid": matched_gid,
                "duplicate_of": "",
            }
        )

    galleries_by_key: dict[tuple[str, str], Gallery] = {}
    if lookup_by_provider:
        provider_gid_q = Q()
        for provider_name, gids in lookup_by_provider.items():
            provider_gid_q |= Q(provider=provider_name, gid__in=list(gids))

        if user_is_authenticated:
            archives_qs = Archive.objects.all().order_by("-public_date", "-pk")
            galleries_qs = Gallery.objects.filter(provider_gid_q).prefetch_related(
                Prefetch("archive_set", queryset=archives_qs, to_attr="visible_main_archives"),
                Prefetch("alternative_sources", queryset=archives_qs, to_attr="visible_alt_archives"),
            )
        else:
            public_archives_qs = Archive.objects.filter(
                public=True,
                binned=False,
                file_deleted=False,
            ).order_by("-public_date", "-pk")
            galleries_qs = Gallery.objects.filter(
                provider_gid_q,
                public=True,
                status=Gallery.StatusChoices.NORMAL,
            ).prefetch_related(
                Prefetch("archive_set", queryset=public_archives_qs, to_attr="visible_main_archives"),
                Prefetch("alternative_sources", queryset=public_archives_qs, to_attr="visible_alt_archives"),
            )

        for fetched_gallery in galleries_qs:
            galleries_by_key[(fetched_gallery.provider, fetched_gallery.gid)] = fetched_gallery

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "total": 0,
        "ready": 0,
        "already_available": 0,
        "submittable": 0,
        "attention": 0,
    }

    for entry in parsed_entries:
        counts["total"] += 1
        kind = entry["kind"]
        raw_input = entry["input"]
        provider_name = entry["provider"]

        if kind == "invalid":
            counts["attention"] += 1
            items.append(
                {
                    "input": raw_input,
                    "provider": "",
                    "status": "invalid",
                    "can_submit": False,
                    "needs_submit": False,
                    "message": "Commands starting with '-' are not allowed.",
                    "gallery": None,
                    "archives": [],
                    "duplicate_of": "",
                }
            )
            continue

        if kind == "duplicate":
            counts["attention"] += 1
            items.append(
                {
                    "input": raw_input,
                    "provider": provider_name,
                    "status": "duplicate",
                    "can_submit": False,
                    "needs_submit": False,
                    "message": "Duplicate of an earlier URL in your list.",
                    "gallery": None,
                    "archives": [],
                    "duplicate_of": entry["duplicate_of"],
                }
            )
            continue

        if kind == "unsupported":
            counts["attention"] += 1
            items.append(
                {
                    "input": raw_input,
                    "provider": "",
                    "status": "unsupported",
                    "can_submit": False,
                    "needs_submit": False,
                    "message": "Unsupported site or not a direct gallery URL.",
                    "gallery": None,
                    "archives": [],
                    "duplicate_of": "",
                }
            )
            continue

        if kind == "note":
            counts["attention"] += 1
            items.append(
                {
                    "input": raw_input,
                    "provider": "",
                    "status": "note",
                    "can_submit": False,
                    "needs_submit": False,
                    "message": "Not a gallery URL (kept as extra note if submitted with valid URLs).",
                    "gallery": None,
                    "archives": [],
                    "duplicate_of": "",
                }
            )
            continue

        gallery = galleries_by_key.get((provider_name, entry["gid"]))

        if gallery is None:
            counts["ready"] += 1
            counts["submittable"] += 1
            items.append(
                {
                    "input": raw_input,
                    "provider": provider_name,
                    "status": "ready",
                    "can_submit": True,
                    "needs_submit": True,
                    "message": (
                        "Not in backup yet — ready to submit."
                        if user_is_authenticated
                        else "Not in public backup — ready to submit."
                    ),
                    "gallery": None,
                    "archives": [],
                    "duplicate_of": "",
                }
            )
            continue

        archive_pairs = _collect_gallery_archives(gallery)
        is_failed = _is_gallery_failed(gallery)
        public_available_archives = [
            (archive, rel) for archive, rel in archive_pairs if _is_archive_public_available(archive)
        ]

        serialized_gallery = _serialize_gallery(gallery, user_is_authenticated=user_is_authenticated)
        serialized_archives = [
            _serialize_archive(archive, rel, user_is_authenticated=user_is_authenticated)
            for archive, rel in archive_pairs
        ]

        is_already_public_complete = bool(
            gallery.public
            and gallery.is_normal()
            and not is_failed
            and len(public_available_archives) > 0
        )

        if is_already_public_complete:
            counts["already_available"] += 1
            items.append(
                {
                    "input": raw_input,
                    "provider": provider_name,
                    "status": "already_available",
                    "can_submit": False,
                    "needs_submit": False,
                    "message": "Already backed up and publicly available — no need to submit.",
                    "gallery": serialized_gallery,
                    "archives": serialized_archives,
                    "duplicate_of": "",
                }
            )
            continue

        if not user_is_authenticated:
            # Gallery is guaranteed public=True and status=NORMAL here, and archives are guaranteed
            # public=True, binned=False, file_deleted=False. Either it has no public archives yet,
            # or its internal dl_type is failed (which we must not expose to anonymous users).
            counts["ready"] += 1
            counts["submittable"] += 1
            if serialized_archives:
                status = "can_submit"
                message = "Gallery is listed — you can still submit it for review."
            else:
                status = "gallery_only"
                message = "Gallery metadata is listed, but no public archive is available yet — you can submit it."
            items.append(
                {
                    "input": raw_input,
                    "provider": provider_name,
                    "status": status,
                    "can_submit": True,
                    "needs_submit": True,
                    "message": message,
                    "gallery": serialized_gallery,
                    "archives": serialized_archives,
                    "duplicate_of": "",
                }
            )
            continue

        # Authenticated caller: expose full internal status cleanly.
        active_archives = [(archive, rel) for archive, rel in archive_pairs if _is_archive_active(archive)]
        if gallery.is_deleted() or gallery.is_denied():
            counts["attention"] += 1
            status = "removed"
            needs_submit = False
            message = f"Gallery exists with status: {gallery.get_status_display()}."
        elif is_failed:
            counts["ready"] += 1
            counts["submittable"] += 1
            status = "failed"
            needs_submit = True
            message = "Gallery previously failed download — can be submitted."
        elif gallery.is_normal() and active_archives:
            counts["attention"] += 1
            status = "private_available"
            needs_submit = False
            message = "Already in backup (private — not publicly available yet)."
        elif gallery.is_submitted():
            counts["attention"] += 1
            status = "in_review"
            needs_submit = False
            message = "Already in the submit queue waiting for review."
        else:
            counts["ready"] += 1
            counts["submittable"] += 1
            status = "gallery_only"
            needs_submit = True
            message = "Gallery exists without an active archive — ready to submit."

        items.append(
            {
                "input": raw_input,
                "provider": provider_name,
                "status": status,
                "can_submit": True,
                "needs_submit": needs_submit,
                "message": message,
                "gallery": serialized_gallery,
                "archives": serialized_archives,
                "duplicate_of": "",
            }
        )

    return {
        "items": items,
        "counts": counts,
        "is_authenticated": user_is_authenticated,
    }


def _check_public_submit_enabled(request: HttpRequest) -> HttpResponse | None:
    if not crawler_settings.urls.enable_public_submit:
        if not request.user.is_staff:
            raise Http404("Page not found")
        return render_error(request, "Page disabled by settings (urls: enable_public_submit).")
    return None


def url_submit_check(request: HttpRequest) -> HttpResponse:
    """AJAX JSON endpoint for live URL status checks on the /submit/ page."""
    if not crawler_settings.urls.enable_public_submit:
        if not request.user.is_staff:
            raise Http404("Page not found")
        return JsonResponse({"error": "Page disabled by settings (urls: enable_public_submit)."}, status=403)

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    raw_lines: list[str] = []
    content_type = request.content_type or ""
    if "application/json" in content_type:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)
        if isinstance(payload.get("urls"), list):
            for entry in payload["urls"]:
                if isinstance(entry, str):
                    stripped = entry.strip()
                    if stripped:
                        raw_lines.append(stripped)
        elif isinstance(payload.get("text"), str):
            raw_lines = parse_submit_lines(payload["text"])
    else:
        raw_lines = parse_submit_lines(request.POST.get("urls", ""))

    user_is_authenticated = bool(request.user.is_authenticated)
    max_urls = AUTHENTICATED_MAX_CHECK_URLS if user_is_authenticated else ANONYMOUS_MAX_CHECK_URLS

    if len(raw_lines) > max_urls:
        return JsonResponse(
            {
                "error": f"Too many URLs to check at once ({len(raw_lines)}). Maximum is {max_urls} per check.",
                "max_urls": max_urls,
            },
            status=400,
        )

    result = inspect_submitted_urls(raw_lines, user_is_authenticated=user_is_authenticated)
    result["max_urls"] = max_urls
    return JsonResponse(result)


def url_submit(request: HttpRequest) -> HttpResponse:
    """Submit given URLs."""
    disabled_resp = _check_public_submit_enabled(request)
    if disabled_resp is not None:
        return disabled_resp

    user_is_authenticated = bool(request.user.is_authenticated)
    p = request.POST

    if p and "submit" in p:
        raw_lines = parse_submit_lines(p.get("urls", ""))
        # Reject commands while keeping non-empty unique lines in original order.
        cleaned_lines: list[str] = []
        seen_lines: set[str] = set()
        for line in raw_lines:
            if line.startswith("-"):
                continue
            if line not in seen_lines:
                seen_lines.add(line)
                cleaned_lines.append(line)

        if not cleaned_lines:
            messages.error(request, "Submission is empty.")
            return HttpResponseRedirect(reverse("viewer:url-submit"))

        reason = p.get("reason", "").strip()

        parsers = [
            parser
            for parser in crawler_settings.provider_context.get_parsers(crawler_settings)
            if parser.id_from_url_implemented()
        ]

        # Match lines to (url, provider, gid), preserving order and deduplicating (provider, gid).
        matched_urls: list[tuple[str, str, str]] = []
        seen_provider_gid: set[tuple[str, str]] = set()
        matched_line_set: set[str] = set()
        lookup_by_provider: dict[str, set[str]] = defaultdict(set)

        for line in cleaned_lines:
            for parser in parsers:
                if parser.filter_accepted_urls([line]):
                    gid = parser.id_from_url(line)
                    if gid:
                        matched_line_set.add(line)
                        key = (parser.name, gid)
                        if key not in seen_provider_gid:
                            seen_provider_gid.add(key)
                            matched_urls.append((line, parser.name, gid))
                            lookup_by_provider[parser.name].add(gid)
                        break

        extra_text = [line for line in cleaned_lines if line not in matched_line_set]

        if not matched_urls:
            messages.warning(request, "No supported gallery URLs were found in your submission.")
            return HttpResponseRedirect(reverse("viewer:url-submit"))

        provider_gid_q = Q()
        for provider_name, gids in lookup_by_provider.items():
            provider_gid_q |= Q(provider=provider_name, gid__in=list(gids))

        all_archives_qs = Archive.objects.all().order_by("-public_date", "-pk")
        galleries_by_key: dict[tuple[str, str], Gallery] = {
            (g.provider, g.gid): g
            for g in Gallery.objects.filter(provider_gid_q).prefetch_related(
                Prefetch("archive_set", queryset=all_archives_qs, to_attr="visible_main_archives"),
                Prefetch("alternative_sources", queryset=all_archives_qs, to_attr="visible_alt_archives"),
            )
        }

        url_messages: list[str] = []
        admin_messages: list[str] = []
        urls_to_submit: list[tuple[str, Gallery | None, str, str]] = []
        already_available_feedback: list[dict[str, Any]] = []
        submitted_feedback: list[dict[str, Any]] = []

        for url_filtered, provider_name, gid in matched_urls:
            gallery = galleries_by_key.get((provider_name, gid))
            archive_pairs = _collect_gallery_archives(gallery) if gallery else []
            public_available_archives = [
                (archive, rel) for archive, rel in archive_pairs if _is_archive_public_available(archive)
            ]
            is_already_public_complete = bool(
                gallery
                and gallery.public
                and gallery.is_normal()
                and not _is_gallery_failed(gallery)
                and len(public_available_archives) > 0
            )

            if is_already_public_complete and gallery is not None:
                # Already publicly backed up and healthy: no need to submit or enqueue!
                visible_archives = archive_pairs if user_is_authenticated else public_available_archives
                already_available_feedback.append(
                    {
                        "url": url_filtered,
                        "provider": provider_name,
                        "gallery": _serialize_gallery(gallery, user_is_authenticated=user_is_authenticated),
                        "archives": [
                            _serialize_archive(a, rel, user_is_authenticated=user_is_authenticated)
                            for a, rel in visible_archives
                        ],
                    }
                )
                messages.info(
                    request,
                    "URL {} is already publicly backed up — no need to submit: {}".format(
                        url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                    ),
                )
                continue

            urls_to_submit.append((url_filtered, gallery, provider_name, gid))

            if not gallery:
                url_messages.append("{}: New URL, will be added to the submit queue".format(url_filtered))
                if user_is_authenticated:
                    messages.success(
                        request,
                        "URL {} was not in the backup and will be added to the submit queue.".format(url_filtered),
                    )
                else:
                    messages.success(request, "URL {} has been submitted for review.".format(url_filtered))
                submitted_feedback.append(
                    {"url": url_filtered, "provider": provider_name, "gallery": None}
                )
                continue

            # Gallery exists in DB (either not public, non-normal, failed, or missing a public archive).
            # Always record full details for admin emails/logs, but NEVER leak non-public details to anonymous users.
            if gallery.is_submitted():
                if gallery.public:
                    url_messages.append(
                        "{}: Already in submit queue but no Archive found, link: {}, reason: {}".format(
                            url_filtered, request.build_absolute_uri(gallery.get_absolute_url()), gallery.reason
                        )
                    )
                    if user_is_authenticated:
                        messages.info(
                            request,
                            "URL {} already exists but a source file isn't available: {}.".format(
                                url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                            ),
                        )
                else:
                    url_messages.append(
                        "{}: Already in submit queue, link: {}, reason: {}".format(
                            url_filtered, request.build_absolute_uri(gallery.get_absolute_url()), gallery.reason
                        )
                    )
                    if user_is_authenticated:
                        messages.info(
                            request,
                            "URL {} already exists in the backup and it's being reviewed.".format(url_filtered),
                        )
            elif gallery.is_deleted():
                url_messages.append(
                    "{}: Was marked as deleted: {}".format(
                        url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                    )
                )
                if user_is_authenticated:
                    messages.info(request, "URL {} was deleted from the backup.".format(url_filtered))
            elif gallery.is_denied():
                url_messages.append(
                    "{}: Was submitted but denied: {}".format(
                        url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                    )
                )
                if user_is_authenticated:
                    messages.info(request, "URL {} was submitted but denied.".format(url_filtered))
            elif gallery.public:
                url_messages.append(
                    "{}: Already present, is public: {}".format(
                        url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                    )
                )
                if user_is_authenticated:
                    messages.info(
                        request,
                        "URL {} already exists in the backup: {}".format(
                            url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                        ),
                    )
            else:
                url_messages.append(
                    "{}: Already present, is not public: {}".format(
                        url_filtered, request.build_absolute_uri(gallery.get_absolute_url())
                    )
                )
                if user_is_authenticated:
                    messages.info(
                        request,
                        "URL {} already exists in the backup and it's being reviewed.".format(url_filtered),
                    )

            if not user_is_authenticated:
                # Uniform, non-leaking confirmation for anonymous users regardless of private/deleted/denied state.
                messages.success(request, "URL {} has been submitted for review.".format(url_filtered))
                public_gallery_data = (
                    _serialize_gallery(gallery, user_is_authenticated=False)
                    if (gallery.public and gallery.is_normal())
                    else None
                )
                submitted_feedback.append(
                    {"url": url_filtered, "provider": provider_name, "gallery": public_gallery_data}
                )
            else:
                submitted_feedback.append(
                    {
                        "url": url_filtered,
                        "provider": provider_name,
                        "gallery": _serialize_gallery(gallery, user_is_authenticated=True),
                    }
                )

        if urls_to_submit:
            current_settings = Settings(load_from_config=crawler_settings.config)
            current_settings.back_up_downloaders = current_settings.downloaders.copy()
            current_settings.replace_metadata = False
            current_settings.config["allowed"]["replace_metadata"] = "no"
            for k in current_settings.config["downloaders"]:
                current_settings.config["downloaders"][k] = str(-1)
                current_settings.downloaders[k] = -1
            current_settings.config["downloaders"]["panda_submit"] = str(1)
            current_settings.config["downloaders"]["nhentai_submit"] = str(1)
            current_settings.downloaders["panda_submit"] = 1
            current_settings.downloaders["nhentai_submit"] = 1

            uuid_group = uuid.uuid4()
            urls_to_enqueue: list[str] = []

            for url_filtered, gallery, _provider_name, _gallery_id in urls_to_submit:
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
                urls_to_enqueue.append(url_filtered)

            current_settings.submit_group_uuid = str(uuid_group)

            client.enqueue(
                args=urls_to_enqueue,
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

            users_to_mail = users_with_perm(
                "viewer",
                "approve_gallery",
                Q(email__isnull=False) | ~Q(email__exact=""),
                profile__notify_new_submissions=True,
            )

            mails = [m for m in users_to_mail.values_list("email", flat=True) if m]

            if mails:
                logger.info("New submission: queueing notification emails for %d enabled user(s).", len(mails))
                body_text = "\n".join(admin_messages)
                body_html = urlize(linebreaks(body_text))
                email_messages = [
                    [
                        admin_subject,
                        body_text,
                        body_html,
                        crawler_settings.mail_logging.from_,
                        [mail],
                    ]
                    for mail in mails
                ]
                client.enqueue(
                    args=[],
                    task_type="send_email_worker",
                    override_options={"messages": email_messages, "fail_silently": True},
                    user=request.user if request.user.is_authenticated else None,
                    reason=reason[:500] if reason else "",
                    deduplicate=False,
                )

            logger.info("{}\n{}".format(admin_subject, "\n".join(admin_messages)))

        if already_available_feedback or submitted_feedback:
            request.session[SUBMIT_FEEDBACK_SESSION_KEY] = {
                "already_available": already_available_feedback,
                "submitted": submitted_feedback,
            }

        return HttpResponseRedirect(reverse("viewer:url-submit"))

    d: dict[str, Any] = {
        "max_check_urls": AUTHENTICATED_MAX_CHECK_URLS if user_is_authenticated else ANONYMOUS_MAX_CHECK_URLS,
        "initial_urls": "",
        "initial_reason": "",
        "submit_feedback": None,
        "check_report": None,
    }

    if p and "check" in p:
        raw_text = p.get("urls", "")
        d["initial_urls"] = raw_text
        d["initial_reason"] = p.get("reason", "")
        raw_lines = parse_submit_lines(raw_text)
        max_urls = d["max_check_urls"]
        if len(raw_lines) > max_urls:
            messages.warning(
                request,
                f"Only the first {max_urls} URLs were checked (maximum {max_urls} per check).",
            )
            raw_lines = raw_lines[:max_urls]
        d["check_report"] = inspect_submitted_urls(raw_lines, user_is_authenticated=user_is_authenticated)
    else:
        d["submit_feedback"] = request.session.pop(SUBMIT_FEEDBACK_SESSION_KEY, None)
        prefill_urls = request.GET.get("urls") or request.GET.get("url") or ""
        if prefill_urls:
            d["initial_urls"] = prefill_urls
        prefill_reason = request.GET.get("reason") or ""
        if prefill_reason:
            d["initial_reason"] = prefill_reason

    return render(request, "viewer/url_submit.html", d)
