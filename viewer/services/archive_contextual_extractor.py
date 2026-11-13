import io
import json
import logging
import mimetypes
import os
import time
import typing
import zipfile
import zlib
from typing import Any

import requests
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from core.base.config_models import (
    DEFAULT_MANGA_EXTRACTOR_ARCHIVE_FIELDS,
    DEFAULT_MANGA_EXTRACTOR_IMAGE_FIELDS,
    MangaExtractorSettings,
    MangaExtractorStorageConfig,
)
from core.base.cancellation import ping_activity
from core.base.utilities import get_images_from_zip

if typing.TYPE_CHECKING:
    from viewer.models import Archive, Image

logger = logging.getLogger(__name__)


LANGUAGE_TAG_MAP: dict[str, str] = {
    "japanese": "ja",
    "english": "en",
    "chinese": "zh",
    "korean": "ko",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "russian": "ru",
    "italian": "it",
    "portuguese": "pt",
}


class ArchiveContextualExtractorService:
    """Service handling contextual extraction of archives and images via manga-extractor REST API."""

    TAG = "manga-extractor"

    @classmethod
    def _resolve_manga_extractor_settings(cls, settings_override: Any = None) -> MangaExtractorSettings:
        if isinstance(settings_override, MangaExtractorSettings):
            return settings_override
        if settings_override is not None and hasattr(settings_override, "manga_extractor"):
            me_cfg = getattr(settings_override, "manga_extractor")
            if isinstance(me_cfg, MangaExtractorSettings):
                return me_cfg
            if isinstance(me_cfg, dict):
                inst = MangaExtractorSettings()
                inst.update_from_dict(me_cfg)
                return inst
        if isinstance(settings_override, dict):
            inst = MangaExtractorSettings()
            if "manga_extractor" in settings_override and isinstance(settings_override["manga_extractor"], dict):
                inst.update_from_dict(settings_override["manga_extractor"])
            else:
                inst.update_from_dict(settings_override)
            return inst
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        if crawler_settings is not None and hasattr(crawler_settings, "manga_extractor"):
            return crawler_settings.manga_extractor
        return MangaExtractorSettings()

    @classmethod
    def resolve_language_hint(cls, archive: "Archive", configured_hint: str) -> str:
        hint = (configured_hint or "").strip().lower()
        if hint and hint != "auto":
            return hint

        try:
            lang_tags = list(archive.tags.filter(scope="language").values_list("name", flat=True))
            for tag_name in lang_tags:
                tag_lower = tag_name.strip().lower()
                if tag_lower in LANGUAGE_TAG_MAP:
                    return LANGUAGE_TAG_MAP[tag_lower]
        except Exception as e:
            logger.debug("Could not inspect archive language tags for %s: %s", archive.pk, e)

        return "ja"

    @classmethod
    def _format_property_value(cls, val: Any, max_len: int = 0) -> str:
        if val is None:
            text = ""
        elif isinstance(val, bool):
            text = "true" if val else "false"
        elif isinstance(val, (dict, list)):
            text = json.dumps(val, ensure_ascii=False)
        else:
            text = str(val)

        if max_len and max_len > 0 and len(text) > max_len:
            text = text[:max_len]
        return text

    FIELD_ALIASES: dict[str, str] = {
        "color_page_count": "color_pages_count",
        "spread_page_count": "spread_pages_count",
        "total_speech_bubbles": "total_bubbles",
        "total_character_count": "total_characters",
        "total_word_count": "total_words",
        "dialogue_density": "average_dialogue_density",
        "is_color": "color_type",
        "speech_bubble_count": "bubble_count",
        "character_count": "char_count",
        "char_density": "visual_density",
        "text_area_ratio": "dialogue_density",
        "full_transcript": "transcript",
        "bubble_transcript": "bubble_transcripts",
        "bubbles_transcripts": "bubble_transcripts",
        "speech_bubble_transcripts": "bubble_transcripts",
        "kanji_count": "japanese_metrics.kanji_count",
        "hiragana_count": "japanese_metrics.hiragana_count",
        "katakana_count": "japanese_metrics.katakana_count",
        "kanji_ratio": "japanese_metrics.kanji_ratio",
        "estimated_jlpt_level": "japanese_metrics.estimated_jlpt_level",
    }

    @classmethod
    def _resolve_path(cls, data: dict[str, Any], path: str) -> tuple[bool, Any]:
        if path in data:
            return True, data[path]
        if "." in path:
            curr: Any = data
            for part in path.split("."):
                if isinstance(curr, dict) and part in curr:
                    curr = curr[part]
                else:
                    return False, None
            return True, curr
        return False, None

    @classmethod
    def _resolve_field_value(cls, data: dict[str, Any], field_path: str) -> tuple[bool, Any]:
        found, val = cls._resolve_path(data, field_path)
        if found:
            return True, val
        alias_path = cls.FIELD_ALIASES.get(field_path)
        if alias_path:
            return cls._resolve_path(data, alias_path)
        return False, None

    @classmethod
    def _select_fields(cls, data: dict[str, Any], allowed_fields: list[str], default_fields: list[str]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        if not allowed_fields:
            fields_to_use = default_fields
        elif "*" in allowed_fields:
            fields_to_use = list(data.keys())
        else:
            fields_to_use = allowed_fields

        selected: dict[str, Any] = {}
        for field_name in fields_to_use:
            found, val = cls._resolve_field_value(data, field_name)
            if found and val is not None:
                selected[field_name] = val
        return selected

    @classmethod
    def save_extraction_results(
        cls,
        archive: "Archive",
        images_by_index: dict[int, "Image"],
        job_data: dict[str, Any],
        storage_config: MangaExtractorStorageConfig,
    ) -> None:
        from viewer.models import Archive as ArchiveModel, Image as ImageModel, ItemProperties

        archive_ct = ContentType.objects.get_for_model(ArchiveModel)
        image_ct = ContentType.objects.get_for_model(ImageModel)
        max_len = storage_config.max_value_length

        # 1. Save archive summary properties
        summary_data = job_data.get("summary")
        if storage_config.store_archive_summary and isinstance(summary_data, dict):
            selected_summary = cls._select_fields(
                summary_data,
                storage_config.archive_fields,
                DEFAULT_MANGA_EXTRACTOR_ARCHIVE_FIELDS,
            )
            archive_props = [
                ItemProperties(
                    content_type=archive_ct,
                    object_id=archive.pk,
                    tag=cls.TAG,
                    name=field_name,
                    value=cls._format_property_value(field_val, max_len=max_len),
                )
                for field_name, field_val in selected_summary.items()
            ]
            if archive_props:
                ItemProperties.objects.bulk_create(
                    archive_props,
                    update_conflicts=True,
                    unique_fields=["content_type", "object_id", "name"],
                    update_fields=["value", "tag"],
                )
            if storage_config.prune_removed_fields:
                ItemProperties.objects.filter(
                    content_type=archive_ct,
                    object_id=archive.pk,
                    tag=cls.TAG,
                ).exclude(name__in=list(selected_summary.keys())).delete()
        elif not storage_config.store_archive_summary and storage_config.prune_removed_fields:
            ItemProperties.objects.filter(
                content_type=archive_ct,
                object_id=archive.pk,
                tag=cls.TAG,
            ).delete()

        # 2. Save per-image properties
        results_list = job_data.get("results")
        if storage_config.store_image_results and isinstance(results_list, list):
            image_props_to_upsert: list[ItemProperties] = []
            saved_names_by_image_pk: dict[int, set[str]] = {}

            for idx, page_result in enumerate(results_list):
                if not isinstance(page_result, dict):
                    continue
                img_idx = page_result.get("image_index", idx)
                try:
                    img_idx_int = int(img_idx)
                except (ValueError, TypeError):
                    img_idx_int = idx

                image_obj = images_by_index.get(img_idx_int)
                if not image_obj or not image_obj.pk:
                    continue

                selected_img_fields = cls._select_fields(
                    page_result,
                    storage_config.image_fields,
                    DEFAULT_MANGA_EXTRACTOR_IMAGE_FIELDS,
                )
                saved_names_by_image_pk[image_obj.pk] = set(selected_img_fields.keys())
                for field_name, field_val in selected_img_fields.items():
                    image_props_to_upsert.append(
                        ItemProperties(
                            content_type=image_ct,
                            object_id=image_obj.pk,
                            tag=cls.TAG,
                            name=field_name,
                            value=cls._format_property_value(field_val, max_len=max_len),
                        )
                    )

            if image_props_to_upsert:
                ItemProperties.objects.bulk_create(
                    image_props_to_upsert,
                    update_conflicts=True,
                    unique_fields=["content_type", "object_id", "name"],
                    update_fields=["value", "tag"],
                )

            if storage_config.prune_removed_fields and saved_names_by_image_pk:
                for img_pk, kept_names in saved_names_by_image_pk.items():
                    ItemProperties.objects.filter(
                        content_type=image_ct,
                        object_id=img_pk,
                        tag=cls.TAG,
                    ).exclude(name__in=list(kept_names)).delete()
        elif not storage_config.store_image_results and storage_config.prune_removed_fields:
            image_pks = [img.pk for img in images_by_index.values() if img.pk]
            if image_pks:
                ItemProperties.objects.filter(
                    content_type=image_ct,
                    object_id__in=image_pks,
                    tag=cls.TAG,
                ).delete()

    @classmethod
    def extract_for_archive(
        cls,
        archive: "Archive",
        settings_override: Any = None,
        profile: str | None = None,
    ) -> bool:
        """Submit archive images to manga-extractor REST API, poll until completion, and store results."""
        me_settings = cls._resolve_manga_extractor_settings(settings_override)
        if not me_settings.enable:
            logger.info("manga_extractor is disabled in settings; skipping archive %s", archive.pk)
            return False

        selected_profile = profile
        if selected_profile is None and settings_override is not None:
            if hasattr(settings_override, "manga_extractor_profile"):
                selected_profile = getattr(settings_override, "manga_extractor_profile")
            elif isinstance(settings_override, dict) and "manga_extractor_profile" in settings_override:
                selected_profile = settings_override["manga_extractor_profile"]
            elif isinstance(settings_override, dict) and "profile" in settings_override:
                selected_profile = settings_override["profile"]

        active_profile = me_settings.get_profile(selected_profile)
        req_cfg = active_profile.request_config
        storage_config = active_profile.storage_config

        if not archive.zipped or not archive.zipped.name or not os.path.isfile(archive.zipped.path):
            logger.warning("Archive %s has no valid zip file on disk", archive.pk)
            return False

        if not archive.image_set.exists():
            archive.generate_image_set()

        images = list(archive.image_set.all().order_by("archive_position"))
        if not images:
            logger.warning("Archive %s has no images to extract contextual info for", archive.pk)
            return False

        activity_cb = getattr(archive, "_activity_callback", None) or getattr(
            settings_override, "activity_callback", None
        )
        is_cancelled_cb = getattr(archive, "_is_cancelled", None) or getattr(
            settings_override, "is_cancelled", None
        )

        def _ping_activity() -> None:
            ping_activity(activity_cb)

        def _check_cancelled() -> bool:
            if callable(is_cancelled_cb):
                try:
                    return bool(is_cancelled_cb())
                except Exception:
                    return False
            return False

        my_zip = None
        try:
            my_zip = zipfile.ZipFile(archive.zipped.path, "r")
        except (zipfile.BadZipFile, NotImplementedError, zlib.error, OSError) as e:
            logger.error("Error opening zipfile for archive %s: %s", archive.pk, e)
            return False

        files_payload: list[tuple[str, tuple[str, bytes, str]]] = []
        images_by_index: dict[int, "Image"] = {}

        try:
            filtered_files = get_images_from_zip(my_zip)
            for idx, (filename_tuple, image_obj) in enumerate(zip(filtered_files, images)):
                _ping_activity()
                if _check_cancelled():
                    logger.info("Contextual extraction cancelled while reading archive %s", archive.pk)
                    return False

                image_filename, nested_zip_filename, _ = filename_tuple
                if nested_zip_filename is None:
                    with my_zip.open(image_filename) as zf_img:
                        img_bytes = zf_img.read()
                else:
                    with my_zip.open(nested_zip_filename) as current_zip:
                        nested_zip_data = io.BytesIO(current_zip.read())
                        with zipfile.ZipFile(nested_zip_data) as my_nested_zip:
                            with my_nested_zip.open(image_filename) as zf_img:
                                img_bytes = zf_img.read()

                basename = os.path.basename(image_filename) or f"page_{idx:04d}.jpg"
                safe_name = f"{idx:04d}_{basename}"
                mime_type, _ = mimetypes.guess_type(basename)
                if not mime_type:
                    mime_type = "application/octet-stream"

                files_payload.append(("files", (safe_name, img_bytes, mime_type)))
                images_by_index[idx] = image_obj
        finally:
            if my_zip:
                my_zip.close()

        if not files_payload:
            logger.warning("No readable images found in archive %s", archive.pk)
            return False

        base_url = me_settings.url.rstrip("/")
        lang_hint = cls.resolve_language_hint(archive, req_cfg.language_hint)

        form_data: dict[str, Any] = {
            "preset": req_cfg.preset,
            "language_hint": lang_hint,
            "detector_backend": req_cfg.detector_backend,
            "use_cache": "true" if req_cfg.use_cache else "false",
        }
        if req_cfg.features:
            form_data["features"] = ",".join(str(f).strip() for f in req_cfg.features if str(f).strip())
        if req_cfg.ocr_max_dimension is not None:
            form_data["ocr_max_dimension"] = str(req_cfg.ocr_max_dimension)
        if req_cfg.ocr_space_threshold is not None:
            form_data["ocr_space_threshold"] = str(req_cfg.ocr_space_threshold)

        headers = me_settings.get_headers() if hasattr(me_settings, "get_headers") else {}

        _ping_activity()
        try:
            submit_resp = requests.post(
                f"{base_url}/api/v1/jobs",
                files=files_payload,
                data=form_data,
                headers=headers,
                timeout=max(30.0, min(me_settings.timeout, 120.0)),
            )
            submit_resp.raise_for_status()
            job_info = submit_resp.json()
        except Exception as e:
            logger.error("Failed to submit job to manga-extractor for archive %s: %s", archive.pk, e)
            return False

        job_id = job_info.get("job_id")
        if not job_id:
            logger.error("manga-extractor returned no job_id for archive %s: %s", archive.pk, job_info)
            return False

        status = job_info.get("status")
        start_time = time.monotonic()
        poll_interval = max(0.2, float(me_settings.poll_interval))
        timeout = max(5.0, float(me_settings.timeout))

        while status in ("queued", "processing"):
            _ping_activity()
            if _check_cancelled():
                logger.info("Cancelling remote manga-extractor job %s for archive %s", job_id, archive.pk)
                try:
                    requests.delete(f"{base_url}/api/v1/jobs/{job_id}", headers=headers, timeout=10.0)
                except Exception as cancel_err:
                    logger.warning("Failed to cancel remote job %s: %s", job_id, cancel_err)
                return False

            if time.monotonic() - start_time > timeout:
                logger.error(
                    "Timed out waiting for manga-extractor job %s on archive %s after %.1fs",
                    job_id,
                    archive.pk,
                    timeout,
                )
                try:
                    requests.delete(f"{base_url}/api/v1/jobs/{job_id}", headers=headers, timeout=10.0)
                except Exception:
                    pass
                return False

            time.sleep(poll_interval)
            _ping_activity()

            try:
                poll_resp = requests.get(f"{base_url}/api/v1/jobs/{job_id}", headers=headers, timeout=30.0)
                poll_resp.raise_for_status()
                job_info = poll_resp.json()
                status = job_info.get("status")
            except Exception as e:
                logger.error("Error polling manga-extractor job %s for archive %s: %s", job_id, archive.pk, e)
                return False

        if status != "completed":
            logger.error(
                "manga-extractor job %s for archive %s finished with status '%s': %s",
                job_id,
                archive.pk,
                status,
                job_info.get("error"),
            )
            return False

        _ping_activity()
        cls.save_extraction_results(archive, images_by_index, job_info, storage_config)
        logger.info(
            "Successfully extracted contextual info for archive %s (job %s, profile: %s)",
            archive.pk,
            job_id,
            selected_profile or me_settings.default_profile,
        )
        return True
