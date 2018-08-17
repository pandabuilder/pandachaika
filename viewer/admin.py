from typing import Optional

from django.db.models import F, QuerySet
from django.forms import ModelForm, BaseFormSet
from django.http import HttpRequest

from viewer.models import (
    Archive,
    Tag,
    Gallery,
    Image,
    WantedGallery,
    Announce,
    Artist,
    GalleryMatch,
    TweetPost,
    FoundGallery,
    Scheduler,
    ArchiveMatches,
    EventLog,
    Provider, Attribute, ArchiveQuerySet, GalleryQuerySet)
from django.contrib import admin
from django.contrib.admin.helpers import ActionForm
from django import forms


class UpdateActionForm(ActionForm):
    extra_field = forms.CharField(required=False)


class ArchiveAdmin(admin.ModelAdmin):
    raw_id_fields = ("gallery", "custom_tags", "tags")
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

    def save_model(self, request: HttpRequest, obj: Archive, form: ModelForm, change: bool) -> None:
        if not obj.user:
            obj.user = request.user
        obj.save()

    def save_related(self, request: HttpRequest, form: ModelForm, formsets: BaseFormSet, change):
        super(ArchiveAdmin, self).save_related(request, form, formsets, change)
        if form.instance.gallery and form.instance.gallery.tags.all():
            form.instance.tags.set(form.instance.gallery.tags.all())


class TagAdmin(admin.ModelAdmin):
    search_fields = ["name", "scope"]
    list_display = ["id", "name", "scope", "source"]
    list_filter = ["source"]


class GalleryAdmin(admin.ModelAdmin):
    search_fields = ["title", "title_jpn"]
    raw_id_fields = ("tags", "gallery_container")
    list_display = ["__str__", "id", "gid", "token",
                    "category", "filesize", "posted", "filecount", "hidden", "dl_type", "provider"]
    list_filter = [
        "category", "expunged", "fjord", "public", "hidden", "dl_type",
        "provider", "status", "origin", "reason"
    ]
    actions = ['make_hidden', 'make_public', 'set_provider']
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

    def set_provider(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        provider = request.POST['extra_field']
        rows_updated = queryset.update(provider=provider)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully set with provider: %s." % (message_bit, provider))
    set_provider.short_description = "Set provider of selected galleries"  # type: ignore


class ImageAdmin(admin.ModelAdmin):
    raw_id_fields = ("archive", )
    exclude = ('image',)
    search_fields = ["archive__title", "sha1"]
    list_display = ["id", "archive", "archive_position", "position", "sha1", "extracted"]
    list_filter = ["extracted"]


class ArtistAdmin(admin.ModelAdmin):
    search_fields = ["name", "name_jpn"]
    list_display = ["id", "name", "name_jpn"]


class AnnounceAdmin(admin.ModelAdmin):
    # search_fields = ["name", "scope"]
    list_display = ["id", "announce_date", "release_date", "type", "source", "comment", "thumbnail"]
    list_filter = ["type", "source"]


class FoundGalleryInline(admin.TabularInline):
    model = FoundGallery
    extra = 2
    raw_id_fields = ("wanted_gallery", "gallery",)


class WantedGalleryAdmin(admin.ModelAdmin):
    search_fields = ["title", "title_jpn"]
    list_display = ["id", "title", "title_jpn", "search_title", "release_date", "book_type",
                    "publisher", "should_search", "keep_searching", "found", "date_found"]
    raw_id_fields = ("announces", "wanted_tags", "unwanted_tags",)
    list_filter = ["book_type", "publisher", "should_search", "keep_searching", "found", "reason", "public"]
    actions = ['make_public', 'mark_should_search', 'mark_not_should_search',
               'mark_keep_search', 'mark_not_keep_search',
               'mark_found', 'mark_not_found',
               'search_title_from_title', 'set_reason']

    action_form = UpdateActionForm

    inlines = (FoundGalleryInline,)

    def make_public(self, request: HttpRequest, queryset: QuerySet) -> None:
        rows_updated = queryset.update(public=True)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked as public." % message_bit)
    make_public.short_description = "Mark selected galleries as public"  # type: ignore

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


admin.site.register(Archive, ArchiveAdmin)
admin.site.register(Tag, TagAdmin)
admin.site.register(Gallery, GalleryAdmin)
admin.site.register(Image, ImageAdmin)

admin.site.register(Artist, ArtistAdmin)
admin.site.register(Announce, AnnounceAdmin)
admin.site.register(WantedGallery, WantedGalleryAdmin)
admin.site.register(GalleryMatch, GalleryMatchAdmin)
admin.site.register(TweetPost, TweetPostAdmin)
admin.site.register(FoundGallery, FoundGalleryAdmin)
admin.site.register(Scheduler, SchedulerAdmin)
admin.site.register(ArchiveMatches, ArchiveMatchesAdmin)
admin.site.register(Provider, ProviderAdmin)
admin.site.register(EventLog, EventLogAdmin)
