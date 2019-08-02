from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Q, Prefetch, F, Case, When
from django.http import HttpRequest, HttpResponse, Http404, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from viewer.utils.actions import event_log
from viewer.forms import ArchiveGroupSearchForm, ArchiveGroupCreateOrEditForm, ArchiveSearchForm, \
    ArchiveGroupEntryFormSet
from viewer.models import ArchiveGroup, ArchiveGroupEntry, Archive

from viewer.views.head import frontend_logger, archive_filter_keys, filter_archives_simple


@login_required
def archive_groups_explorer(request: HttpRequest) -> HttpResponse:
    get = request.GET

    title = get.get("title", '')

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    if 'clear' in get:
        form = ArchiveGroupSearchForm()
    else:
        form = ArchiveGroupSearchForm(initial={'title': title})

    d = {
        'form': form,
    }

    if request.user.has_perm('viewer.add_archivegroup'):
        if request.POST.get('submit-archive-group'):
            # create a form instance and populate it with data from the request:
            edit_form = ArchiveGroupCreateOrEditForm(request.POST)
            # check whether it's valid:
            if edit_form.is_valid():
                new_archive_group = edit_form.save()
                message = 'New archive group successfully created'
                messages.success(request, message)
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                event_log(
                    request.user,
                    'ADD_ARCHIVE_GROUP',
                    content_object=new_archive_group,
                    result='created'
                )
            else:
                messages.error(request, 'The provided data is not valid', extra_tags='danger')
                # return HttpResponseRedirect(request.META["HTTP_REFERER"])
        else:
            edit_form = ArchiveGroupCreateOrEditForm()

        d.update(edit_form=edit_form)

    order = 'position'

    results = ArchiveGroup.objects.order_by(F(order).asc(nulls_last=True))

    if not request.user.is_authenticated:
        results = results.filter(public=True)

    q_formatted = '%' + title.replace(' ', '%') + '%'
    results = results.filter(
        Q(title__ss=q_formatted)
    )

    results = results.prefetch_related(
        Prefetch(
            'archivegroupentry_set',
            queryset=ArchiveGroupEntry.objects.select_related('archive_group', 'archive').prefetch_related(
                Prefetch(
                    'archive__tags',
                )
            ),
            to_attr='archivegroup_entries'
        ),
    )

    paginator = Paginator(results, 50)
    try:
        results = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results = paginator.page(paginator.num_pages)

    d.update(results=results)

    return render(request, "viewer/archive_groups.html", d)


@login_required
def archive_group(request: HttpRequest, pk: int = None, slug: str = None) -> HttpResponse:
    """ArchiveGroup listing."""
    try:
        if pk is not None:
            archive_group_instance = ArchiveGroup.objects.get(pk=pk)
        elif slug is not None:
            archive_group_instance = ArchiveGroup.objects.get(title_slug=slug)
        else:
            raise Http404("Archive Group does not exist")
    except ArchiveGroup.DoesNotExist:
        raise Http404("Archive Group does not exist")
    if not archive_group_instance.public and not request.user.is_authenticated:
        raise Http404("Archive Group does not exist")

    d = {
        'archive_group': archive_group_instance,
    }

    return render(request, "viewer/archive_group.html", d)


@permission_required('viewer.change_archivegroup')
def archive_group_edit(request: HttpRequest, pk: int = None, slug: str = None) -> HttpResponse:
    """ArchiveGroup listing."""
    try:
        if pk is not None:
            archive_group_instance = ArchiveGroup.objects.prefetch_related('archivegroupentry_set__archive').get(pk=pk)
        elif slug is not None:
            archive_group_instance = ArchiveGroup.objects.prefetch_related('archivegroupentry_set__archive').get(title_slug=slug)
        else:
            raise Http404("Archive Group does not exist")
    except ArchiveGroup.DoesNotExist:
        raise Http404("Archive Group does not exist")
    if not archive_group_instance.public and not request.user.is_authenticated:
        raise Http404("Archive Group does not exist")

    get = request.GET
    p = request.POST

    user_reason = p.get('reason', '')

    d = {
        'archive_group': archive_group_instance,
    }

    if request.POST.get('submit-archive-group'):
        # create a form instance and populate it with data from the request:
        edit_form = ArchiveGroupCreateOrEditForm(request.POST, instance=archive_group_instance)
        archive_group_entry_formset = ArchiveGroupEntryFormSet(request.POST, instance=archive_group_instance)

        # check whether it's valid:
        if edit_form.is_valid() and archive_group_entry_formset.is_valid():
            new_archive_group = edit_form.save()

            # archive_group_entry_formset.save()
            archive_group_entries = archive_group_entry_formset.save(commit=False)
            for archive_group_entry in archive_group_entries:
                archive_group_entry.save()
            for archive_group_entry in archive_group_entry_formset.deleted_objects:
                archive_group_entry.delete()

            message = 'Archive group successfully modified'
            messages.success(request, message)
            frontend_logger.info("User {}: {}".format(request.user.username, message))
            event_log(
                request.user,
                'CHANGE_ARCHIVE_GROUP',
                content_object=new_archive_group,
                result='changed'
            )
            return HttpResponseRedirect(reverse('viewer:archive-group-edit', args=[archive_group_instance.title_slug]))
        else:
            messages.error(request, 'The provided data is not valid', extra_tags='danger')

        # archive_group_entry_formset = ArchiveGroupEntryFormSet(
        #     request.POST,
        #     initial=[{'archive_group_id': archive_group_instance.id}] * 2,
        #     queryset=ArchiveGroupEntry.objects.filter(archive_group=archive_group_instance),
        #     prefix='archive_group_entries'
        # )

    else:
        edit_form = ArchiveGroupCreateOrEditForm(instance=archive_group_instance)
        archive_group_entry_formset = ArchiveGroupEntryFormSet(instance=archive_group_instance)

    if 'add_to_group' in p:

        pks = []
        for k, v in p.items():
            if k.startswith("sel-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        archives = Archive.objects.filter(id__in=pks).order_by(preserved)

        for archive in archives:
            if not ArchiveGroupEntry.objects.filter(archive=archive, archive_group=archive_group_instance).exists():

                archive_group_entry = ArchiveGroupEntry(archive=archive, archive_group=archive_group_instance)
                archive_group_entry.save()

                message = 'Adding archive: {}, link: {}, to group: {}, link {}'.format(
                    archive.title, archive.get_absolute_url(),
                    archive_group_instance.title, archive_group_instance.get_absolute_url()
                )
                if 'reason' in p and p['reason'] != '':
                    message += ', reason: {}'.format(p['reason'])
                frontend_logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)
                event_log(
                    request.user,
                    'ADD_ARCHIVE_TO_GROUP',
                    content_object=archive,
                    reason=user_reason,
                    result='added'
                )

        return HttpResponseRedirect(
            reverse(
                'viewer:archive-group-edit', args=[archive_group_instance.title_slug]
            ) + '?' + request.META['QUERY_STRING']
        )

    d.update(edit_form=edit_form)

    # Multi add search form

    title = get.get("title", '')
    tags = get.get("tags", '')

    try:
        page = int(get.get("page", '1'))
    except ValueError:
        page = 1

    if 'add_multiple' in get:
        if 'clear' in get:
            search_form = ArchiveSearchForm()
        else:
            search_form = ArchiveSearchForm(initial={'title': title, 'tags': tags})

        params = {
            'sort': get.get("sort", 'create_date'),
            'asc_desc': get.get("asc_desc", 'desc'),
        }

        for k, v in get.items():
            params[k] = v

        for k in archive_filter_keys:
            if k not in params:
                params[k] = ''

        search_results = filter_archives_simple(params)

        search_results = search_results.exclude(archive_groups=archive_group_instance)

        if 'groupless' in get and get['groupless']:
            search_results = search_results.filter(archive_groups__isnull=True)

        search_results = search_results.prefetch_related('gallery')

        paginator = Paginator(search_results, 100)
        try:
            search_results = paginator.page(page)
        except (InvalidPage, EmptyPage):
            search_results = paginator.page(paginator.num_pages)

        d.update(
            search_form=search_form,
            search_results=search_results,
        )

    d.update(
        archive_group_entry_formset=archive_group_entry_formset
    )

    return render(request, "viewer/archive_group_edit.html", d)
