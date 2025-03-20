from typing import Optional

from django.db.models import F, QuerySet, Count
from django.forms import ModelForm, BaseFormSet
from django.http import HttpRequest
from django.utils.dateparse import parse_duration
from django.utils.translation import gettext_lazy as _
from django.conf import settings
from simple_history.admin import SimpleHistoryAdmin


from viewer.utils.actions import event_log
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
    Provider,
    Attribute,
    ArchiveQuerySet,
    GalleryQuerySet,
    GallerySubmitEntry,
    ArchiveManageEntry,
    ArchiveRecycleEntry,
    MonitoredLink,
    TagQuerySet,
    GalleryProviderData,
    ItemProperties,
    UserLongLivedToken,
    ProcessedLinks,
    ArchiveOption,
    WantedImage,
    DownloadEvent,
    Category,
)
from django.contrib import admin
from django.contrib.admin.helpers import ActionForm
from django import forms

from core.base.setup import Settings
from viewer.utils.types import AuthenticatedHttpRequest


crawler_settings = settings.CRAWLER_SETTINGS


class UpdateActionForm(ActionForm):
    extra_field = forms.CharField(required=False)


class ArchiveImageInline(admin.TabularInline):
    model = Image
    extra = 0
    # raw_id_fields = ("archive_group", "archive",)


class ArchiveAdmin(SimpleHistoryAdmin):
    raw_id_fields = ("gallery", "tags", "alternative_sources")
    search_fields = ["title", "title_jpn", "zipped"]
    list_display = ["title", "zipped", "gallery_id", "filesize", "filecount", "create_date"]
    list_filter = [
        "user",
        "match_type",
        "source_type",
        "public",
        "gallery__provider",
        "gallery__hidden",
        "reason",
        "origin",
        "extracted",
        "binned",
    ]
    actions = ["make_public", "mark_source_custom", "set_reason", "mark_origin"]
    action_form = UpdateActionForm
    list_select_related = ("gallery",)
    inlines = (ArchiveImageInline,)

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
        source_type = request.POST["extra_field"]
        rows_updated = queryset.update(source_type=source_type)
        if rows_updated == 1:
            message_bit = "1 archive was"
        else:
            message_bit = "%s archives were" % rows_updated
        self.message_user(request, "%s successfully set as %s source." % (message_bit, source_type))

    mark_source_custom.short_description = "Update source of selected archives"  # type: ignore

    # TODO: Use the string value, not the integer.
    def mark_origin(self, request: HttpRequest, queryset: ArchiveQuerySet) -> None:
        origin = request.POST["extra_field"]
        rows_updated = queryset.update(origin=origin)
        if rows_updated == 1:
            message_bit = "1 archive was"
        else:
            message_bit = "%s archives were" % rows_updated
        self.message_user(request, "%s successfully set as %s origin." % (message_bit, origin))

    mark_origin.short_description = "Update origin of selected archives"  # type: ignore

    def set_reason(self, request: HttpRequest, queryset: ArchiveQuerySet) -> None:
        source_type = request.POST["extra_field"]
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
        if not obj.user and isinstance(request, AuthenticatedHttpRequest):
            obj.user = request.user
        obj.save()

    def save_related(self, request: HttpRequest, form: ModelForm, formsets: BaseFormSet, change):
        super(ArchiveAdmin, self).save_related(request, form, formsets, change)
        if form.instance.gallery and form.instance.gallery.tags.all():
            form.instance.set_tags_from_gallery(form.instance.gallery)


class ArchiveGroupEntryInline(admin.TabularInline):
    model = ArchiveGroupEntry
    extra = 2
    raw_id_fields = (
        "archive_group",
        "archive",
    )


class ArchiveGroupAdmin(admin.ModelAdmin):
    search_fields = ["title", "archive_group__title"]
    list_display = ["id", "title", "position", "public", "create_date"]
    list_filter = ["public"]

    inlines = (ArchiveGroupEntryInline,)


class ArchiveGroupEntryAdmin(admin.ModelAdmin):
    search_fields = ["title", "archive_group__title"]
    raw_id_fields = ("archive",)
    list_display = ["id", "archive_group", "archive", "title", "position"]


class GalleryTagsInline(admin.TabularInline):
    model = Gallery.tags.through
    raw_id_fields = ("gallery",)


class UsedByGalleryListFilter(admin.SimpleListFilter):
    title = _("used by galleries")  # type: ignore
    parameter_name = "used-galleries"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Yes")),
            ("no", _("No")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.annotate(num_gallery=Count("gallery")).filter(num_gallery__gt=0)
        if self.value() == "no":
            return queryset.annotate(num_gallery=Count("gallery")).filter(num_gallery=0)


class TagAdmin(admin.ModelAdmin):
    search_fields = ["name", "scope"]
    list_display = ["id", "name", "scope", "source"]
    list_filter = ["source", UsedByGalleryListFilter, "gallery__provider"]
    inlines = [
        GalleryTagsInline,
    ]
    actions = ["recall_api_gallery"]

    def recall_api_gallery(self, request: HttpRequest, queryset: TagQuerySet) -> None:
        galleries = Gallery.objects.filter(tags__in=queryset)
        for gallery in galleries:
            current_settings = Settings(load_from_config=crawler_settings.config)

            if current_settings.workers.web_queue and gallery.provider:
                current_settings.set_update_metadata_options(providers=(gallery.provider,))

                def gallery_callback(x: Optional["Gallery"], crawled_url: Optional[str], result: str) -> None:
                    event_log(request.user, "UPDATE_METADATA", content_object=x, result=result, data=crawled_url)

                current_settings.workers.web_queue.enqueue_args_list(
                    (gallery.get_link(),), override_options=current_settings, gallery_callback=gallery_callback
                )

        self.message_user(request, "%s galleries pulled new metadata." % galleries.count())

    recall_api_gallery.short_description = "Recall Gallery API to all related galleries"  # type: ignore


class GalleryProviderDataInline(admin.TabularInline):
    model = GalleryProviderData
    extra = 1


class GalleryAdmin(SimpleHistoryAdmin):
    search_fields = ["title", "title_jpn", "gid"]
    raw_id_fields = ("tags", "gallery_container", "magazine", "first_gallery", "parent_gallery")
    list_display = [
        "__str__",
        "id",
        "gid",
        "token",
        "category",
        "filesize",
        "posted",
        "create_date",
        "last_modified",
        "filecount",
        "hidden",
        "dl_type",
        "provider",
    ]
    list_filter = [
        "category",
        "expunged",
        "disowned",
        "fjord",
        "public",
        "hidden",
        "dl_type",
        "provider",
        "status",
        "origin",
        "reason",
    ]
    actions = ["make_hidden", "make_public", "make_private", "set_provider", "set_reason", "make_normal"]
    action_form = UpdateActionForm
    inlines = (GalleryProviderDataInline,)

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
        rows_updated = queryset.update(status=Gallery.StatusChoices.NORMAL)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully marked status as normal." % message_bit)

    make_normal.short_description = "Mark selected galleries status as normal"  # type: ignore

    def set_provider(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        provider = request.POST["extra_field"]
        rows_updated = queryset.update(provider=provider)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully set with provider: %s." % (message_bit, provider))

    set_provider.short_description = "Set provider of selected galleries"  # type: ignore

    def set_reason(self, request: HttpRequest, queryset: GalleryQuerySet) -> None:
        reason = request.POST["extra_field"]
        rows_updated = queryset.update(reason=reason)
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully set with reason: %s." % (message_bit, reason))

    set_reason.short_description = "Set reason of selected galleries"  # type: ignore


class ImageAdmin(admin.ModelAdmin):
    raw_id_fields = ("archive",)
    exclude = ("image",)
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


class WantedImageAdmin(admin.ModelAdmin):
    exclude = (
        "thumbnail",
        "thumbnail_height",
        "thumbnail_width",
        "sha1",
        "image_width",
        "image_height",
        "image_format",
        "image_mode",
        "image_size",
    )
    search_fields = ["image_name", "sha1"]
    list_display = ["id", "active", "image_name", "minimum_features", "match_threshold"]
    list_filter = ["active"]


class FoundGalleryInline(admin.TabularInline):
    model = FoundGallery
    extra = 2
    raw_id_fields = (
        "wanted_gallery",
        "gallery",
    )


class WantedGalleryAdmin(admin.ModelAdmin):
    search_fields = ["title", "title_jpn"]
    list_display = [
        "id",
        "title",
        "search_title",
        "release_date",
        "book_type",
        "publisher",
        "wait_for_time",
        "should_search",
        "keep_searching",
        "found",
        "date_found",
    ]
    raw_id_fields = ("mentions", "wanted_tags", "unwanted_tags", "artists", "cover_artist")
    list_filter = [
        "book_type",
        "publisher",
        "should_search",
        "keep_searching",
        "found",
        "reason",
        "public",
        "notify_when_found",
        "wanted_providers",
        "unwanted_providers",
        "wait_for_time",
        "category",
        "categories",
    ]
    actions = [
        "make_public",
        "mark_should_search",
        "mark_not_should_search",
        "mark_keep_search",
        "mark_not_keep_search",
        "mark_found",
        "mark_not_found",
        "search_title_from_title",
        "set_reason",
        "add_wanted_provider",
        "add_unwanted_provider",
        "remove_wanted_provider",
        "remove_unwanted_provider",
        "enable_notify_when_found",
        "disable_notify_when_found",
        "set_wait_for_time",
        "set_category",
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
        rows_updated = queryset.update(search_title=F("title"))
        if rows_updated == 1:
            message_bit = "1 gallery was"
        else:
            message_bit = "%s galleries were" % rows_updated
        self.message_user(request, "%s successfully set search title from title." % message_bit)

    search_title_from_title.short_description = "Set selected galleries' search title from title"  # type: ignore

    def set_reason(self, request: HttpRequest, queryset: QuerySet) -> None:
        source_type = request.POST["extra_field"]
        rows_updated = queryset.update(reason=source_type)
        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(request, "%s successfully set as reason: %s." % (message_bit, source_type))

    set_reason.short_description = "Set reason of selected wanted galleries"  # type: ignore

    def add_wanted_provider(self, request: HttpRequest, queryset: QuerySet) -> None:
        wanted_provider_slug = request.POST["extra_field"]
        provider = Provider.objects.filter(slug=wanted_provider_slug).first()
        rows_updated = 0
        if provider:
            missing_provider = queryset.exclude(wanted_providers=provider)
            for wanted_gallery in missing_provider:
                wanted_gallery.wanted_providers.add(provider)
            rows_updated = missing_provider.count()

        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(request, "%s successfully added wanted provider: %s." % (message_bit, wanted_provider_slug))

    add_wanted_provider.short_description = "Add wanted provider by slug name"  # type: ignore

    def remove_wanted_provider(self, request: HttpRequest, queryset: QuerySet) -> None:
        wanted_provider_slug = request.POST["extra_field"]
        provider = Provider.objects.filter(slug=wanted_provider_slug).first()
        rows_updated = 0
        if provider:
            present_provider = queryset.filter(wanted_providers=provider)
            for wanted_gallery in present_provider:
                wanted_gallery.wanted_providers.remove(provider)
            rows_updated = present_provider.count()

        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(request, "%s successfully removed wanted provider: %s." % (message_bit, wanted_provider_slug))

    remove_wanted_provider.short_description = "Remove wanted provider by slug name"  # type: ignore

    def add_unwanted_provider(self, request: HttpRequest, queryset: QuerySet) -> None:
        unwanted_provider_slug = request.POST["extra_field"]
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
        self.message_user(
            request, "%s successfully added unwanted provider: %s." % (message_bit, unwanted_provider_slug)
        )

    add_unwanted_provider.short_description = "Add unwanted provider by slug name"  # type: ignore

    def remove_unwanted_provider(self, request: HttpRequest, queryset: QuerySet) -> None:
        unwanted_provider_slug = request.POST["extra_field"]
        provider = Provider.objects.filter(slug=unwanted_provider_slug).first()
        rows_updated = 0
        if provider:
            present_provider = queryset.filter(unwanted_providers=provider)
            for wanted_gallery in present_provider:
                wanted_gallery.unwanted_providers.remove(provider)
            rows_updated = present_provider.count()

        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(
            request, "%s successfully removed unwanted provider: %s." % (message_bit, unwanted_provider_slug)
        )

    remove_unwanted_provider.short_description = "Remove unwanted provider by slug name"  # type: ignore

    def set_wait_for_time(self, request: HttpRequest, queryset: QuerySet) -> None:
        wait_for_time = request.POST["extra_field"]
        rows_updated = queryset.update(wait_for_time=parse_duration(wait_for_time))
        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(request, "%s successfully set wait for time to: %s." % (message_bit, wait_for_time))

    set_wait_for_time.short_description = "Set wait for time of selected wanted galleries"  # type: ignore

    def set_category(self, request: HttpRequest, queryset: QuerySet) -> None:
        category = request.POST["extra_field"]
        rows_updated = queryset.update(category=category)
        if rows_updated == 1:
            message_bit = "1 wanted gallery was"
        else:
            message_bit = "%s wanted galleries were" % rows_updated
        self.message_user(request, "%s successfully set category to: %s." % (message_bit, category))

    set_category.short_description = "Set category of selected wanted galleries"  # type: ignore


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
    raw_id_fields = (
        "wanted_gallery",
        "gallery",
    )
    list_display = ["id", "wanted_gallery", "gallery", "match_accuracy", "source", "create_date"]
    # list_filter = ["source"]


class SchedulerAdmin(admin.ModelAdmin):
    search_fields = ["name", "description"]
    list_display = ["id", "name", "description", "last_run"]


class ArchiveMatchesAdmin(admin.ModelAdmin):
    search_fields = ["archive__title", "archive__title_jpn", "gallery__title", "gallery__title_jpn"]
    raw_id_fields = (
        "archive",
        "gallery",
    )
    list_display = ["id", "archive", "gallery", "match_accuracy"]
    list_filter = ["gallery__provider"]


class AttributeInline(admin.TabularInline):
    model = Attribute
    extra = 2


class ProviderAdmin(admin.ModelAdmin):
    search_fields = ["name", "description"]
    list_display = ["id", "name", "description", "home_page"]

    inlines = (AttributeInline,)


class CategoryAdmin(admin.ModelAdmin):
    search_fields = ["name", "slug"]
    list_display = ["id", "name", "slug"]


class EventLogAdmin(admin.ModelAdmin):

    raw_id_fields = ["user"]
    list_filter = ["action", "create_date", "content_type"]
    list_display = ["create_date", "user", "action", "reason", "data", "result", "content_type", "object_id"]
    search_fields = ["user__username", "user__email", "reason", "result", "object_id"]


class UserLongLivedTokenAdmin(admin.ModelAdmin):

    raw_id_fields = ["user"]
    list_display = ["user", "name", "create_date", "expire_date"]
    list_filter = ["create_date", "expire_date"]
    search_fields = ["user__username", "user__email"]


class ProcessedLinksAdmin(admin.ModelAdmin):

    list_filter = ["link_date", "create_date", "provider"]
    list_display = ["source_id", "url", "title", "link_date", "create_date"]
    search_fields = ["url"]


class ItemPropertiesAdmin(admin.ModelAdmin):

    list_filter = ["name", "tag", "content_type"]
    list_display = ["name", "tag", "value", "content_type", "object_id"]
    search_fields = ["name", "tag", "value"]


class GallerySubmitEntryAdmin(admin.ModelAdmin):

    raw_id_fields = ["gallery", "similar_galleries"]
    list_filter = ["submit_result", "resolved_status"]
    list_display = [
        "gallery",
        "submit_url",
        "submit_reason",
        "submit_date",
        "resolved_status",
        "resolved_reason",
        "resolved_date",
    ]
    search_fields = ["gallery__title", "submit_reason", "resolved_reason"]


class ArchiveManageEntryAdmin(admin.ModelAdmin):

    raw_id_fields = ["archive"]
    list_filter = ["mark_check", "mark_user", "resolve_check", "resolve_user", "mark_date", "origin", "mark_reason"]
    list_display = [
        "archive",
        "mark_check",
        "mark_priority",
        "mark_reason",
        "mark_user",
        "mark_extra",
        "resolve_check",
        "resolve_user",
    ]
    search_fields = ["mark_reason", "mark_extra", "mark_comment", "resolve_comment"]


class ArchiveRecycleEntryAdmin(admin.ModelAdmin):

    raw_id_fields = ["archive"]
    list_filter = ["user", "date_deleted", "origin"]
    list_display = ["archive", "reason", "user"]
    search_fields = ["reason", "comment"]


class ArchiveOptionAdmin(admin.ModelAdmin):

    raw_id_fields = ["archive"]
    list_filter = ["freeze_titles", "freeze_tags"]
    list_display = ["archive", "freeze_titles", "freeze_tags"]


class MonitoredLinkAdmin(admin.ModelAdmin):

    raw_id_fields = ["limited_wanted_galleries"]
    list_filter = ["enabled", "auto_start", "provider"]
    list_display = ["name", "provider", "enabled", "auto_start", "create_date"]
    search_fields = ["name", "url"]
    actions = ["start_running", "stop_running", "force_run"]

    def start_running(self, request: HttpRequest, queryset: "QuerySet[MonitoredLink]") -> None:
        rows_updated = queryset.count()
        for monitored_link in queryset:
            monitored_link.start_running()
        if rows_updated == 1:
            message_bit = "1 monitored link was"
        else:
            message_bit = "%s monitored links were" % rows_updated
        self.message_user(request, "%s successfully started." % message_bit)

    start_running.short_description = "Start selected MonitoredLinks"  # type: ignore

    def stop_running(self, request: HttpRequest, queryset: "QuerySet[MonitoredLink]") -> None:
        rows_updated = queryset.count()
        for monitored_link in queryset:
            monitored_link.stop_running()
        if rows_updated == 1:
            message_bit = "1 monitored link was"
        else:
            message_bit = "%s monitored links were" % rows_updated
        self.message_user(request, "%s successfully stopped." % message_bit)

    stop_running.short_description = "Stop selected MonitoredLinks"  # type: ignore

    def force_run(self, request: HttpRequest, queryset: "QuerySet[MonitoredLink]") -> None:
        rows_updated = queryset.count()
        for monitored_link in queryset:
            monitored_link.force_run()
        if rows_updated == 1:
            message_bit = "1 monitored link was"
        else:
            message_bit = "%s monitored links were" % rows_updated
        self.message_user(request, "%s successfully force run." % message_bit)

    force_run.short_description = "Force run selected MonitoredLinks"  # type: ignore


class DownloadEventAdmin(admin.ModelAdmin):

    raw_id_fields = ["archive", "gallery"]
    list_filter = ["completed", "failed", "method"]
    list_display = [
        "id",
        "name",
        "archive",
        "progress",
        "total_size",
        "failed",
        "completed",
        "method",
        "download_id",
        "create_date",
        "completed_date",
    ]
    actions = ["mark_as_failed_completed"]

    def mark_as_failed_completed(self, request: HttpRequest, queryset: "QuerySet[DownloadEvent]") -> None:
        rows_updated = queryset.count()
        queryset.update(completed=True, failed=True)
        if rows_updated == 1:
            message_bit = "1 download event was"
        else:
            message_bit = "%s download events were" % rows_updated
        self.message_user(request, "%s successfully marked as failed, completed." % message_bit)

    mark_as_failed_completed.short_description = "Mark as failed, completed"  # type: ignore


admin.site.register(Archive, ArchiveAdmin)
admin.site.register(ArchiveGroup, ArchiveGroupAdmin)
admin.site.register(ArchiveGroupEntry, ArchiveGroupEntryAdmin)
admin.site.register(Tag, TagAdmin)
admin.site.register(Gallery, GalleryAdmin)
admin.site.register(Image, ImageAdmin)

admin.site.register(Artist, ArtistAdmin)
admin.site.register(Mention, MentionAdmin)
admin.site.register(WantedImage, WantedImageAdmin)
admin.site.register(WantedGallery, WantedGalleryAdmin)
admin.site.register(GalleryMatch, GalleryMatchAdmin)
admin.site.register(TweetPost, TweetPostAdmin)
admin.site.register(FoundGallery, FoundGalleryAdmin)
admin.site.register(Scheduler, SchedulerAdmin)
admin.site.register(ArchiveMatches, ArchiveMatchesAdmin)
admin.site.register(Provider, ProviderAdmin)
admin.site.register(EventLog, EventLogAdmin)
admin.site.register(UserLongLivedToken, UserLongLivedTokenAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(ProcessedLinks, ProcessedLinksAdmin)
admin.site.register(ItemProperties, ItemPropertiesAdmin)
admin.site.register(GallerySubmitEntry, GallerySubmitEntryAdmin)
admin.site.register(ArchiveManageEntry, ArchiveManageEntryAdmin)
admin.site.register(ArchiveRecycleEntry, ArchiveRecycleEntryAdmin)
admin.site.register(ArchiveOption, ArchiveOptionAdmin)
admin.site.register(MonitoredLink, MonitoredLinkAdmin)
admin.site.register(DownloadEvent, DownloadEventAdmin)
