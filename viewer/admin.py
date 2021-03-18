from typing import Optional

from django.db.models import F, QuerySet
from django.forms import ModelForm, BaseFormSet
from django.http import HttpRequest

from viewer.models import (
    Archive,
    ArchiveGroup,
    ArchiveGroupEntry,
    Tag,
    Gallery,
    Image,
    WantedGallery,
    Mention,
    Artist,
    GalleryMatch,
    TweetPost,
    FoundGallery,
    Scheduler,
    ArchiveMatches,
    EventLog,
    Provider, Attribute, ArchiveQuerySet, GalleryQuerySet, GallerySubmitEntry)
from django.contrib import admin
from django.contrib.admin.helpers import ActionForm
from django import forms

from viewer.utils.types import AuthenticatedHttpRequest


class UpdateActionForm(ActionForm):
    extra_field = forms.CharField(required=False)


class ArchiveAdmin(admin.ModelAdmin):
    raw_id_fields = ("gallery", "custom_tags", "tags", "alternative_sources")
    search_fields = ["title", "title_jpn"]
    list_display = ["title", "zipped", "gallery_id", "filesize",
                    "filecount", "create_date"]
    list_filter = ["user", "match_type", "source_type", "public", "gallery__hidden", "reason"]
    actions = ['make_public', 'mark_source_fakku', 'mark_source_fakku_sub', 'mark_source_cafe',
               'mark_source_custom', 'set_reason']
    action_form = UpdateActionForm

    def make_public(self, request: HttpRequest, queryset: ArchiveQuerySet) -> None:
        rows_updated = queryset.count()
        for archive in queryset:
            archive.set_public()
        if rows_updated == 1:
            message_bit = "1 archive was"
        else:
            message_bit = "%s archives were" % rows_updated
        self.message_user(request, "%s successfully marked as public." % message_bit)
    make_public.short_description = "Mark selected archives as public"  # type: ignore

    def mark_source_custom(self, request: HttpRequest, queryset: ArchiveQuerySet) -> None:
        source_type = request.POST['extra_field']
        rows_updated = queryset.update(source_type=source_type)
        if rows_updated == 1:
            message_bit = "1 archive was"
        else:
            message_bit = "%s archives were" % rows_updated
        self.message_user(request, "%s successfully set as %s source." % (message_bit, source_type))
    mark_source_custom.short_description = "Update source of selected archives"  # type: ignore

    def set_reason(self, request: HttpRequest, queryset: ArchiveQuerySet) -> None:
        source_type = request.POST['extra_field']
        rows_updated = queryset.update(reason=source_type)
        if rows_updated == 1:
            message_bit = "1 archive was"
        else:
            message_bit = "%s archives were" % rows_updated
        self.message_user(request, "%s successfully set as reason: %s." % (message_bit, source_type))
    set_reason.short_description = "Set reason of selected archives"  # type: ignore

    def gallery_id(self, obj: Archive) -> Optional[int]:
        if obj.gallery:
            return obj.gallery.id
        else:
            return None

    def save_model(self, request: AuthenticatedHttpRequest, obj: Archive, form: ModelForm, change: bool) -> None:
        if not obj.user:
            obj.user = request.user
        obj.save()

    def save_related(self, request: HttpRequest, form: ModelForm, formsets: BaseFormSet, change):
        super(ArchiveAdmin, self).save_related(request, form, formsets, change)
        if form.instance.gallery and form.instance.gallery.tags.all():
            form.instance.tags.set(form.instance.gallery.tags.all())


class ArchiveGroupEntryInline(admin.TabularInline):
    model = ArchiveGroupEntry
    extra = 2
    raw_id_fields = ("archive_group", "archive",)


class ArchiveGroupAdmin(admin.ModelAdmin):
    search_fields = ["title", "archive_group__title"]
    list_display = ["id", "title", "position", "public", "create_date"]
    list_filter = ["public"]

    inlines = (ArchiveGroupEntryInline,)


class ArchiveGroupEntryAdmin(admin.ModelAdmin):
    search_fields = ["title", "archive_group__title"]
    raw_id_fields = ("archive", )
    list_display = ["id", "archive_group", "archive", "title", "position"]


class TagAdmin(admin.ModelAdmin):
    search_fields = ["name", "scope"]
    list_display = ["id", "name", "scope", "source"]
    list_filter = ["source"]


class GalleryAdmin(admin.ModelAdmin):
    search_fields = ["title", "title_jpn", "gid"]
    raw_id_fields = ("tags", "gallery_container", "magazine")
    list_display = [
        "__str__", "id", "gid", "token",
        "category", "filesize", "posted", "create_date", "last_modified",
        "filecount", "hidden", "dl_type", "provider"
    ]
    list_filter = [
        "category", "expunged", "fjord", "public", "hidden", "dl_type",
        "provider", "status", "origin", "reason"
    ]
    actions = ['make_hidden', 'make_public', 'make_private', 'set_provider', 'set_reason', 'make_normal']
    action_form = UpdateActionForm

    def make_hidden(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        rows_updated = queryset.update(hidden=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as hidden." % message_bit)
    make_hidden.short_description = "Mark selected galleries as hidden"  # type: ignore

    def make_public(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        rows_updated = queryset.update(public=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as public." % message_bit)
    make_public.short_description = "Mark selected galleries as public"  # type: ignore

    def make_private(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        rows_updated = queryset.update(public=False)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as private." % message_bit)
    make_private.short_description = "Mark selected galleries as private"  # type: ignore

    def make_normal(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        rows_updated = queryset.update(status=Gallery.NORMAL)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked status as normal." % message_bit)
    make_normal.short_description = "Mark selected galleries status as normal"  # type: ignore

    def set_provider(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        provider = request.POST['extra_field']
        rows_updated = queryset.update(provider=provider)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully set with provider: %s." % (message_bit, provider))
    set_provider.short_description = "Set provider of selected galleries"  # type: ignore

    def set_reason(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        reason = request.POST['extra_field']
        rows_updated = queryset.update(reason=reason)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully set with reason: %s." % (message_bit, reason))
    set_reason.short_description = "Set reason of selected galleries"  # type: ignore


class ImageAdmin(admin.ModelAdmin):
    raw_id_fields = ("archive", )
    exclude = ('image',)
    search_fields = ["archive__title", "sha1"]
    list_display = ["id", "archive", "archive_position", "position", "sha1", "extracted"]
    list_filter = ["extracted"]


class ArtistAdmin(admin.ModelAdmin):
    search_fields = ["name", "name_jpn"]
    list_display = ["id", "name", "name_jpn"]


class MentionAdmin(admin.ModelAdmin):
    # search_fields = ["name", "scope"]
    list_display = ["id", "mention_date", "release_date", "type", "source", "comment", "thumbnail"]
    list_filter = ["type", "source"]


class FoundGalleryInline(admin.TabularInline):
    model = FoundGallery
    extra = 2
    raw_id_fields = ("wanted_gallery", "gallery",)


class WantedGalleryAdmin(admin.ModelAdmin):
    search_fields = ["title", "title_jpn"]
    list_display = ["id", "title", "title_jpn", "search_title", "release_date", "book_type",
                    "publisher", "should_search", "keep_searching", "found", "date_found"]
    raw_id_fields = ("mentions", "wanted_tags", "unwanted_tags", "artists", "cover_artist")
    list_filter = [
        "book_type", "publisher", "should_search", "keep_searching",
        "found", "reason", "public", "notify_when_found", "wanted_providers", "unwanted_providers"
    ]
    actions = ['make_public', 'mark_should_search', 'mark_not_should_search',
               'mark_keep_search', 'mark_not_keep_search',
               'mark_found', 'mark_not_found',
               'search_title_from_title', 'set_reason', 'add_unwanted_provider',
               'enable_notify_when_found', 'disable_notify_when_found'
               ]

    action_form = UpdateActionForm

    # inlines = (FoundGalleryInline,)

    def make_public(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(public=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as public." % message_bit)
    make_public.short_description = "Mark selected galleries as public"  # type: ignore

    def enable_notify_when_found(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(notify_when_found=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully enabled for notify when found." % message_bit)
    enable_notify_when_found.short_description = "Enable selected galleries for notify when found"  # type: ignore

    def disable_notify_when_found(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(notify_when_found=False)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully disabled for notify when found." % message_bit)
    disable_notify_when_found.short_description = "Disable selected galleries for notify when found"  # type: ignore

    def mark_should_search(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(should_search=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as should search." % message_bit)
    mark_should_search.short_description = "Mark selected galleries as should search"  # type: ignore

    def mark_not_should_search(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(should_search=False)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as not should search." % message_bit)
    mark_not_should_search.short_description = "Mark selected galleries as not should search"  # type: ignore

    def mark_found(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(found=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as found." % message_bit)
    mark_found.short_description = "Mark selected galleries as found"  # type: ignore

    def mark_not_found(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(found=False)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as not found." % message_bit)
    mark_not_found.short_description = "Mark selected galleries as not found"  # type: ignore

    def mark_keep_search(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(keep_searching=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as keep searching." % message_bit)
    mark_keep_search.short_description = "Mark selected galleries as keep searching"  # type: ignore

    def mark_not_keep_search(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(keep_searching=False)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as not keep searching." % message_bit)
    mark_not_keep_search.short_description = "Mark selected galleries as not keep searching"  # type: ignore

    def search_title_from_title(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(search_title=F('title'))
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully set search title from title." % message_bit)
    search_title_from_title.short_description = "Set selected galleries' search title from title"  # type: ignore

    def set_reason(self, request: HttpRequest, queryset: QuerySet) -> None:
        source_type = request.POST['extra_field']
        rows_updated = queryset.update(reason=source_type)
        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(request, "%s successfully set as reason: %s." % (message_bit, source_type))
    set_reason.short_description = "Set reason of selected wanted galleries"  # type: ignore

    def add_unwanted_provider(self, request: HttpRequest, queryset: QuerySet) -> None:
        unwanted_provider_slug = request.POST['extra_field']
        provider = Provider.objects.filter(slug=unwanted_provider_slug).first()
        rows_updated = 0
        if provider:
            missing_provider = queryset.exclude(unwanted_providers=provider)
            for wanted_gallery in missing_provider:
                wanted_gallery.unwanted_providers.add(provider)
            rows_updated = missing_provider.count()

        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(request, "%s successfully added unwanted provider: %s." % (message_bit, unwanted_provider_slug))
    add_unwanted_provider.short_description = "Add unwanted provider by slug name"  # type: ignore


class GalleryMatchAdmin(admin.ModelAdmin):
    # search_fields = ["name", "scope"]
    list_display = ["id", "wanted_gallery", "gallery", "match_accuracy"]
    # list_filter = ["source"]


class TweetPostAdmin(admin.ModelAdmin):
    search_fields = ["user", "text"]
    list_display = ["id", "tweet_id", "user", "text", "posted_date", "media_url"]
    list_filter = ["user"]


class FoundGalleryAdmin(admin.ModelAdmin):
    # search_fields = ["name", "scope"]
    raw_id_fields = ("wanted_gallery", "gallery",)
    list_display = ["id", "wanted_gallery", "gallery", "match_accuracy", "source", "create_date"]
    # list_filter = ["source"]


class SchedulerAdmin(admin.ModelAdmin):
    search_fields = ["name", "description"]
    list_display = ["id", "name", "description", "last_run"]


class ArchiveMatchesAdmin(admin.ModelAdmin):
    search_fields = ["archive__title", "archive__title_jpn", "gallery__title", "gallery__title_jpn"]
    raw_id_fields = ("archive", "gallery",)
    list_display = ["id", "archive", "gallery", "match_accuracy"]
    list_filter = ["gallery__provider"]


class AttributeInline(admin.TabularInline):
    model = Attribute
    extra = 2


class ProviderAdmin(admin.ModelAdmin):
    search_fields = ["name", "description"]
    list_display = ["id", "name", "description", "home_page"]

    inlines = (AttributeInline,)


class EventLogAdmin(admin.ModelAdmin):

    raw_id_fields = ["user"]
    list_filter = ["action", "create_date"]
    list_display = ["create_date", "user", "action", "reason", "data", "result"]
    search_fields = ["user__username", "user__email", "reason", "result"]


class GallerySubmitEntryAdmin(admin.ModelAdmin):

    raw_id_fields = ["gallery"]
    list_filter = ["submit_result", "resolved_status"]
    list_display = ["gallery", "submit_url", "submit_reason", "submit_date", "resolved_status", "resolved_reason", "resolved_date"]
    search_fields = ["gallery__title", "submit_reason", "resolved_reason"]


admin.site.register(Archive, ArchiveAdmin)
admin.site.register(ArchiveGroup, ArchiveGroupAdmin)
admin.site.register(ArchiveGroupEntry, ArchiveGroupEntryAdmin)
admin.site.register(Tag, TagAdmin)
admin.site.register(Gallery, GalleryAdmin)
admin.site.register(Image, ImageAdmin)

admin.site.register(Artist, ArtistAdmin)
admin.site.register(Mention, MentionAdmin)
admin.site.register(WantedGallery, WantedGalleryAdmin)
admin.site.register(GalleryMatch, GalleryMatchAdmin)
admin.site.register(TweetPost, TweetPostAdmin)
admin.site.register(FoundGallery, FoundGalleryAdmin)
admin.site.register(Scheduler, SchedulerAdmin)
admin.site.register(ArchiveMatches, ArchiveMatchesAdmin)
admin.site.register(Provider, ProviderAdmin)
admin.site.register(EventLog, EventLogAdmin)
admin.site.register(GallerySubmitEntry, GallerySubmitEntryAdmin)
