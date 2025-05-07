import logging
from typing import Optional, Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Q, Prefetch, F, Case, When
from django.http import HttpRequest, HttpResponse, Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from viewer.utils.actions import event_log
from viewer.forms import (
    GalleryMatchGroupSearchForm,
    GalleryMatchGroupCreateOrEditForm,
    GallerySearchForm,
    GalleryMatchGroupEntryFormSet,
    GallerySearchSimpleForm,
    DivErrorList,
)
from viewer.models import GalleryMatchGroupEntry, Gallery, GalleryMatchGroup

from viewer.views.head import gallery_filter_keys, filter_galleries_simple, gallery_list_filters_keys

logger = logging.getLogger(__name__)


@login_required
def gallery_match_groups_explorer(request: HttpRequest) -> HttpResponse:
    get = request.GET

    title = get.get("title", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if "clear" in get:
        form = GalleryMatchGroupSearchForm()
    else:
        form = GalleryMatchGroupSearchForm(initial={"title": title})

    d: dict[str, Any] = {
        "form": form,
    }

    if request.user.has_perm("viewer.add_gallerymatchgroup"):
        if request.POST.get("submit-gallery-match-group"):
            # create a form instance and populate it with data from the request:
            edit_form = GalleryMatchGroupCreateOrEditForm(request.POST)
            # check whether it's valid:
            if edit_form.is_valid():
                new_gallery_match_group = edit_form.save()
                message = "New gallery match group successfully created"
                messages.success(request, message)
                logger.info("User {}: {}".format(request.user.username, message))
                event_log(request.user, "ADD_GALLERY_MATCH_GROUP", content_object=new_gallery_match_group, result="created")
            else:
                messages.error(request, "The provided data is not valid", extra_tags="danger")
                # return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        else:
            edit_form = GalleryMatchGroupCreateOrEditForm()

        d.update(edit_form=edit_form)

    order = "create_date"

    results = GalleryMatchGroup.objects.order_by(F(order).asc(nulls_last=True))

    # if not request.user.is_authenticated:
    #     results = results.filter(public=True)

    q_formatted = "%" + title.replace(" ", "%") + "%"
    results = results.filter(Q(title__ss=q_formatted))

    results = results.prefetch_related(
        Prefetch(
            "gallerymatchgroupentry_set",
            queryset=GalleryMatchGroupEntry.objects.select_related("gallery_match_group", "gallery").prefetch_related(
                Prefetch(
                    "gallery__tags",
                )
            ),
            to_attr="gallerymatchgroup_entries",
        ),
    )

    paginator = Paginator(results, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d.update(results=results_page)

    return render(request, "viewer/gallery_match_groups.html", d)


@login_required
def gallery_match_group_details(request: HttpRequest, pk: Optional[int] = None) -> HttpResponse:
    """GalleryMatchGroup listing."""
    try:
        if pk is not None:
            gallery_match_group_instance = GalleryMatchGroup.objects.get(pk=pk)
        else:
            raise Http404("Gallery Match Group does not exist")
    except GalleryMatchGroup.DoesNotExist:
        raise Http404("Gallery Match Group does not exist")

    get = request.GET

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    results = GalleryMatchGroupEntry.objects.filter(gallery_match_group=gallery_match_group_instance).select_related("gallery")

    paginator = Paginator(results, 48)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {
        "gallery_match_group": gallery_match_group_instance,
        "results": results_page,
    }

    return render(request, "viewer/gallery_match_group.html", d)


@permission_required("viewer.change_gallerymatchgroup")
def gallery_match_group_edit(request: HttpRequest, pk: Optional[int] = None) -> HttpResponse:
    """GalleryMatchGroup listing."""
    try:
        if pk is not None:
            gallery_match_group_instance = GalleryMatchGroup.objects.get(pk=pk)
        else:
            raise Http404("Gallery Match Group does not exist")
    except GalleryMatchGroup.DoesNotExist:
        raise Http404("Gallery Match Group does not exist")

    get = request.GET
    p = request.POST

    user_reason = p.get("reason", "")

    d: dict[str, Any] = {
        "gallery_match_group": gallery_match_group_instance,
    }

    results = GalleryMatchGroupEntry.objects.filter(gallery_match_group=gallery_match_group_instance).select_related("gallery")

    if request.POST.get("submit-gallery-match-group"):
        # create a form instance and populate it with data from the request:
        edit_form = GalleryMatchGroupCreateOrEditForm(request.POST, instance=gallery_match_group_instance)
        gallery_match_group_entry_formset = GalleryMatchGroupEntryFormSet(request.POST, instance=gallery_match_group_instance)

        # check whether it's valid:
        if edit_form.is_valid() and gallery_match_group_entry_formset.is_valid():
            new_gallery_match_group = edit_form.save()

            # gallery_match_group_entry_formset.save()
            gallery_match_group_entry_formset.save(commit=False)
            for form in gallery_match_group_entry_formset.ordered_forms:
                form.instance.gallery_position = form.cleaned_data["ORDER"]
                form.instance.save()
            # for gallery_match_group_entry in gallery_match_group_entries:
            #     gallery_match_group_entry.save()
            for gallery_match_group_entry in gallery_match_group_entry_formset.deleted_objects:
                gallery_match_group_entry.delete()

            message = "Gallery Match Group successfully modified"
            messages.success(request, message)
            logger.info("User {}: {}".format(request.user.username, message))
            event_log(request.user, "CHANGE_GALLERY_MATCH_GROUP", content_object=new_gallery_match_group, result="changed")
            return HttpResponseRedirect(reverse("viewer:gallery-match-group-edit", args=[gallery_match_group_instance.pk]))
        else:
            messages.error(request, "The provided data is not valid", extra_tags="danger")

        # gallery_match_group_entry_formset = GalleryMatchGroupEntryFormSet(
        #     request.POST,
        #     initial=[{'gallery_match_group_id': gallery_match_group_instance.id}] * 2,
        #     queryset=GalleryMatchGroupEntry.objects.filter(gallery_match_group=gallery_match_group_instance),
        #     prefix='gallery_match_group_entries'
        # )

    else:
        edit_form = GalleryMatchGroupCreateOrEditForm(instance=gallery_match_group_instance)
        gallery_match_group_entry_formset = GalleryMatchGroupEntryFormSet(instance=gallery_match_group_instance, queryset=results)

    if "add_to_group" in p:

        pks = []
        for k, v in p.items():
            if k.startswith("sel-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        galleries = Gallery.objects.filter(id__in=pks).order_by(preserved)

        for gallery in galleries:
            if not GalleryMatchGroupEntry.objects.filter(gallery=gallery, gallery_match_group=gallery_match_group_instance).exists():

                gallery_match_group_entry = GalleryMatchGroupEntry(gallery=gallery, gallery_match_group=gallery_match_group_instance)
                gallery_match_group_entry.save()

                gallery_match_group_instance.save()

                message = "Adding gallery: {}, link: {}, to group: {}, link {}".format(
                    gallery.title,
                    gallery.get_absolute_url(),
                    gallery_match_group_instance.title,
                    gallery_match_group_instance.get_absolute_url(),
                )
                if "reason" in p and p["reason"] != "":
                    message += ", reason: {}".format(p["reason"])
                logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)
                event_log(
                    request.user, "ADD_GALLERY_TO_MATCH_GROUP", content_object=gallery, reason=user_reason, result="added"
                )

        return HttpResponseRedirect(
            reverse("viewer:gallery-match-group-edit", args=[gallery_match_group_instance.pk])
            + "?"
            + request.META["QUERY_STRING"]
        )

    d.update(edit_form=edit_form)

    # Multi add search form

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    paginator = Paginator(results, 48)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    if "add_multiple" in get:
        if "clear" in get:
            search_form = GallerySearchForm()
            form_simple = GallerySearchSimpleForm(initial={})
        else:
            search_form = GallerySearchForm(initial={"title": title, "tags": tags})
            form_simple = GallerySearchSimpleForm(request.GET, error_class=DivErrorList)

        params: dict[str, list[str] | str] = {
            "sort": get.get("sort", "create_date"),
            "asc_desc": get.get("asc_desc", "desc"),
        }

        for k in get:
            if k in gallery_list_filters_keys:
                v_k: list[str] | str = get.getlist(k)
            else:
                v_k = get[k]
            if isinstance(v_k, str) or isinstance(v_k, list):
                params[k] = v_k

        for k in gallery_filter_keys:
            if k not in params:
                params[k] = ""

        search_results = filter_galleries_simple(params)

        search_results = search_results.exclude(gallery_group=gallery_match_group_instance)

        if "groupless" in get and get["groupless"]:
            search_results = search_results.filter(gallery_group__isnull=True)

        # search_results = search_results.select_related("gallery")

        gallery_paginator = Paginator(search_results, 100)
        try:
            search_results_page = gallery_paginator.page(page)
        except (InvalidPage, EmptyPage):
            search_results_page = gallery_paginator.page(paginator.num_pages)

        d.update(
            search_form=search_form,
            form_simple=form_simple,
            search_results=search_results_page,
        )

    d.update(gallery_match_group_entry_formset=gallery_match_group_entry_formset, results=results_page)

    return render(request, "viewer/gallery_match_group_edit.html", d)


@permission_required("viewer.change_gallerymatchgroup")
def gallery_match_group_enter_reason(
    request: HttpRequest, pk: Optional[int] = None, tool: Optional[str] = None
) -> HttpResponse:
    try:
        if pk is not None:
            gallery_match_group_instance = GalleryMatchGroup.objects.get(pk=pk)
        else:
            raise Http404("Gallery Match Group does not exist")
    except GalleryMatchGroup.DoesNotExist:
        raise Http404("Gallery Match Group does not exist")

    if request.method == "POST":

        p = request.POST
        user_reason = p.get("reason", "")
        if "confirm_tool" in p:

            if request.user.has_perm("viewer.delete_gallerymatchgroup") and tool == "delete":
                delete_report = gallery_match_group_instance.delete_text_report()

                message = "Deleting Gallery Match Group: {}".format(gallery_match_group_instance.title or gallery_match_group_instance.pk)

                logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                gallery_match_group_instance.delete()
                event_log(
                    request.user, "DELETE_GALLERY_MATCH_GROUP", reason=user_reason, result="deleted", data=delete_report
                )

                return HttpResponseRedirect(reverse("viewer:gallery-match-groups"))

    d = {"gallery_match_group": gallery_match_group_instance, "tool": tool}

    inlined = request.GET.get("inline", None)

    if inlined:
        return render(request, "viewer/include/modals/gallery_match_group_tool_reason.html", d)
    else:
        return render(request, "viewer/gallery_match_group_display_tool.html", d)
