# -*- coding: utf-8 -*-
import fnmatch
import logging
import os
import re
import time
import argparse
import zlib
from typing import NoReturn
from zipfile import BadZipFile
from zipfile import ZipFile

from core.base.setup import Settings
from core.base.types import DataDict
from core.base.utilities import calc_crc32, get_zip_fileinfo, replace_illegal_name
from core.matching.engine import MatchingEngine, MatchingOptions
from core.matching.types import MatchingMode, MatchTarget

from viewer.models import Archive

logger = logging.getLogger(__name__)


class ArgumentParserError(Exception):
    pass


class YieldingArgumentParser(argparse.ArgumentParser):

    def error(self, message: str) -> NoReturn:
        raise ArgumentParserError(message)


class FolderCrawler(object):

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.parse_error = False

    def get_args(self, arg_line: list[str]) -> argparse.Namespace | ArgumentParserError:

        parser = YieldingArgumentParser(prog="PandaBackupFolder")

        parser.add_argument(
            "folder",
            # action='append',
            nargs="*",
            default=[],
            help="folders to crawl. Must be relative to the media_url",
        )

        parser.add_argument(
            "-reason",
            "--set-reason",
            required=False,
            action="store",
            help="Most parameters are set from the gallery, but reason is user-defined."
            "Can be set afterwards, but this option sets it when crawling. For new files only",
        )

        parser.add_argument(
            "-source",
            "--set-source",
            required=False,
            action="store",
            help="Override the default source_type for " "archives added from the filesystem (default: folder)",
        )

        parser.add_argument(
            "-dmf",
            "--display-missing-files",
            required=False,
            action="store_true",
            help="Display archives missing from the filesystem",
        )

        parser.add_argument(
            "-rmf",
            "--remove-missing-files",
            required=False,
            action="store_true",
            help="Remove from the database archives missing from the filesystem",
        )

        parser.add_argument(
            "-frm",
            "--force-rematch",
            required=False,
            action="store_true",
            help="Force rematch of files, disregarding the match_type",
        )

        parser.add_argument(
            "-rnm",
            "--rematch-non-matches",
            required=False,
            action="store_true",
            help="Rematch archives that could not be matched",
        )

        parser.add_argument(
            "-rtt",
            "--rename-to-title",
            required=False,
            action="store",
            nargs="+",
            metavar="action/paths",
            help=(
                "Rename archives in the filesystem to their title. "
                'If the first option is "rename", the rename will be effective. '
                "Otherwise, it will just display the possible rename. "
                "Must specify the paths to analyze. This is based on the relative path"
            ),
        )

        parser.add_argument(
            "-aftt",
            "--all-filenames-to-title",
            required=False,
            action="store",
            # default='display',
            metavar="action",
            help=(
                "Rename all archives that their basename doesn't match with the "
                'title of the archive. If the action is "rename", the rename will be effective. '
                "Otherwise, it will just display the possible rename"
            ),
        )

        parser.add_argument(
            "-dmfigt",
            "--display-match-from-internal-gallery-titles",
            required=False,
            action="store",
            type=float,
            metavar="cutoff",
            help=(
                "Try to match archives marked as non-matches with"
                " the local galleries database, based on the file name. "
                "Needs a cutoff value to discard likeness. "
                "This will only display the matches, not apply them"
            ),
        )

        parser.add_argument(
            "-rfigt",
            "--rematch-from-internal-gallery-titles",
            required=False,
            action="store",
            type=float,
            metavar="cutoff",
            help=(
                "Try to match archives marked as non-matches "
                "with the local galleries database, based on the file name. "
                "Needs a cutoff value to discard likeness. This will apply the changes"
            ),
        )

        parser.add_argument(
            "-mm",
            "--match-mode",
            required=False,
            choices=[mode.value for mode in MatchingMode],
            default=MatchingMode.ONLINE.value,
            help=(
                "How files are matched. online (default): search the providers with the enabled matchers. "
                "offline: compare with the galleries already in the local database"
            ),
        )

        parser.add_argument(
            "-rmc",
            "--recalc-missing-crc32",
            required=False,
            action="store_true",
            help="Recalculate file info for archives with missing CRC32",
        )

        parser.add_argument(
            "-rbm",
            "--rematch-by-match-type",
            required=False,
            action="store",
            metavar="match_type",
            help="Rematch archives that have a certain match_type",
        )

        parser.add_argument(
            "-rwf",
            "--rematch-wrong-filesize",
            required=False,
            action="store_true",
            help="Rematch archives that their filesize is " "different from the gallery filesize (unpacked)",
        )
        try:
            args = parser.parse_args(arg_line)
            self.parse_error = False
        except ArgumentParserError as e:
            self.parse_error = True
            return e

        if args.rename_to_title:
            if args.rename_to_title[0] == "rename":
                args.folder = args.rename_to_title[1:]
            else:
                args.folder = args.rename_to_title

        return args

    def start_crawling(self, arg_line: list[str]) -> None:

        args = self.get_args(arg_line)

        if isinstance(args, ArgumentParserError):
            logger.info(str(args))
            return

        files = []
        do_not_replace = False
        values: DataDict = {}

        if args.remove_missing_files:
            found_archives = Archive.objects.all()

            if found_archives:
                logger.info("Checking {} archives for existence in filesystem".format(found_archives.count()))
                for archive in found_archives:
                    if not os.path.isfile(archive.zipped.path):
                        Archive.objects.delete_by_filter(pk=archive.pk)

            return
        elif args.display_missing_files:

            found_archives = Archive.objects.all()

            if found_archives:
                logger.info("Checking {} archives for existence in filesystem".format(found_archives.count()))
                for archive in found_archives:
                    if not os.path.isfile(archive.zipped.path):
                        logger.info("Filename: {} doesn't exist".format(archive.zipped.path))
            return
        elif args.rematch_non_matches:
            self.settings.rematch_file_list = ["non-match"]
            self.settings.rematch_file = True
            found_archives = Archive.objects.filter(match_type="non-match")
            if found_archives:
                logger.info("Scanning {} archives with non-matches".format(found_archives.count()))
                for archive in found_archives:
                    if os.path.isfile(archive.zipped.path):
                        files.append(archive.zipped.path)

        elif args.rematch_by_match_type:
            self.settings.rematch_file_list = [args.rematch_by_match_type]
            self.settings.rematch_file = True
            self.settings.replace_metadata = True
            found_archives = Archive.objects.filter(match_type=args.rematch_by_match_type)
            if found_archives:
                logger.info(
                    "Scanning {} archives matched by {}".format(found_archives.count(), args.rematch_by_match_type)
                )
                for archive in found_archives:
                    if os.path.isfile(archive.zipped.path):
                        files.append(archive.zipped.path)
        elif args.rematch_wrong_filesize:
            self.settings.rematch_file = True
            self.settings.replace_metadata = True
            do_not_replace = True
            found_archives = Archive.objects.exclude(match_type="non-match", gallery_id__isnull=True)
            if found_archives:
                for archive in found_archives:
                    if not os.path.isfile(archive.zipped.path):
                        continue
                    if not archive.gallery or archive.filesize == archive.gallery.filesize:
                        continue
                    files.append(archive.zipped.path)
                logger.info("Scanning {} archives matched with wrong filesize".format(len(files)))
        elif args.recalc_missing_crc32:

            found_archives = Archive.objects.filter(crc32="")

            if found_archives:
                logger.info("Calculating {} archives with missing CRC32".format(found_archives.count()))
                for cnt, archive in enumerate(found_archives):
                    if os.path.isfile(archive.zipped.path):
                        crc32 = calc_crc32(archive.zipped.path, use_pool=True)
                        logger.info(
                            "Working on archive {} of {}, CRC32: {}".format((cnt + 1), found_archives.count(), crc32)
                        )
                        values = {"crc32": crc32}
                        Archive.objects.add_or_update_from_values(values, pk=archive.pk)
                    else:
                        logger.info(
                            "Archive {} of {}, path: {} does not exist".format(
                                (cnt + 1), found_archives.count(), archive.zipped.path
                            )
                        )
            return
        elif args.all_filenames_to_title:

            archives_filename_to_title = Archive.objects.exclude(title="")

            if archives_filename_to_title:
                logger.info("Checking {} archives".format(archives_filename_to_title.count()))
                for cnt, archive in enumerate(archives_filename_to_title):
                    if not archive.title:
                        continue
                    current_path = os.path.join(
                        os.path.dirname(archive.zipped.path), replace_illegal_name(archive.title) + ".zip"
                    )

                    if archive.zipped.path != current_path and not os.path.isfile(
                        os.path.join(self.settings.MEDIA_ROOT, current_path)
                    ):
                        logger.info("Filename should be {} but it's {}".format(current_path, archive.zipped.path))
                        if args.filename_to_title == "rename":
                            os.rename(archive.zipped.path, os.path.join(self.settings.MEDIA_ROOT, current_path))
                            values = {
                                "zipped": current_path,
                            }
                            Archive.objects.add_or_update_from_values(values, pk=archive.pk)
            return

        elif args.rematch_from_internal_gallery_titles or args.display_match_from_internal_gallery_titles:
            apply_matches = bool(args.rematch_from_internal_gallery_titles)
            cutoff = float(args.rematch_from_internal_gallery_titles or args.display_match_from_internal_gallery_titles)

            non_matched_archives = Archive.objects.filter(match_type="non-match", gallery__isnull=True)
            logger.info(
                "Matching against the local gallery database, {} archives with no match".format(
                    non_matched_archives.count()
                )
            )
            engine = MatchingEngine(
                self.settings,
                MatchingOptions(
                    mode=MatchingMode.OFFLINE,
                    methods=("title",),
                    cutoff=cutoff,
                    max_matches=1,
                    auto_link=True,
                    auto_link_cutoff=cutoff,
                ),
            )
            for archive in non_matched_archives:
                if not archive.zipped.name:
                    continue
                outcome = engine.process(MatchTarget.from_archive(archive, use_title=False))
                if outcome.best is None:
                    continue
                logger.info("Path: {}\nGal title: {}".format(archive.zipped.name, outcome.best.title))
                if apply_matches:
                    engine.link_archive(archive, outcome.best)
            logger.info("Local gallery matching ended: {}".format(engine.report.summary()))
            return
        else:
            for folder in args.folder:
                p = os.path.normpath(os.path.join(self.settings.MEDIA_ROOT, folder))
                if not p.startswith(self.settings.MEDIA_ROOT):
                    continue
                folder = os.path.relpath(p, self.settings.MEDIA_ROOT).replace("\\", "/")
                if os.path.isdir(os.path.join(self.settings.MEDIA_ROOT, folder)):
                    for root, _, filenames in os.walk(os.path.join(self.settings.MEDIA_ROOT, str(folder))):
                        for filename_filter in self.settings.filename_filter:
                            for filename in fnmatch.filter(filenames, filename_filter):
                                files.append(os.path.relpath(os.path.join(root, filename), self.settings.MEDIA_ROOT))
                elif os.path.isfile(os.path.join(self.settings.MEDIA_ROOT, folder)):
                    files.append(folder)

        if args.rename_to_title:
            logger.info("Checking {} archives".format(len(files)))
            for cnt, filepath in enumerate(files):

                archive_to_rename = Archive.objects.filter(zipped=filepath).first()

                if archive_to_rename and archive_to_rename.title:
                    current_path = os.path.join(
                        os.path.dirname(filepath), replace_illegal_name(archive_to_rename.title) + ".zip"
                    )

                    if filepath != current_path and not os.path.isfile(
                        os.path.join(self.settings.MEDIA_ROOT, current_path)
                    ):
                        logger.info("Filename should be {} but it's {}".format(current_path, filepath))
                        if args.rename_to_title == "rename":
                            os.rename(
                                os.path.join(self.settings.MEDIA_ROOT, filepath),
                                os.path.join(self.settings.MEDIA_ROOT, current_path),
                            )
                            values = {
                                "zipped": current_path,
                            }
                            Archive.objects.add_or_update_from_values(values, zipped=filepath)
            return

        if args.set_reason:
            self.settings.archive_reason = args.set_reason

        if args.set_source:
            self.settings.archive_source = args.set_source

        # The creation of the files list ends here. From here onwards, it's processing them.

        if len(files) == 0:
            logger.info("No file matching needed, skipping matchers")
        else:
            logger.info("Starting checks for {} archives".format(len(files)))

            match_mode = MatchingMode(args.match_mode)
            engine = MatchingEngine(
                self.settings,
                MatchingOptions(mode=match_mode, auto_link=True, cutoff=0.4),
            )
            logger.info("Matching files with {}".format(engine))
            # Possible matches for the files that couldn't be matched. Offline mode already has them.
            internal_engine: MatchingEngine | None = None

            for cnt, filepath in enumerate(files):

                logger.info("Checking file: {} of {}, path: {}".format((cnt + 1), len(files), filepath))

                title = re.sub("[_]", " ", os.path.splitext(os.path.basename(filepath))[0])
                archive_to_process = Archive.objects.filter(zipped=filepath).first()
                if not self.settings.rehash_files and archive_to_process:
                    crc32 = archive_to_process.crc32
                else:
                    crc32 = calc_crc32(os.path.join(self.settings.MEDIA_ROOT, filepath))

                if archive_to_process:
                    if args.force_rematch:
                        logger.info("Doing a forced rematch")
                    elif (
                        archive_to_process.match_type in self.settings.rematch_file_list or args.rematch_wrong_filesize
                    ):
                        if self.settings.rematch_file:
                            logger.info("File was already matched before, but rematch is ordered")
                        else:
                            logger.info("File was already matched before, not rematching")
                            continue
                    else:
                        logger.info("Match already saved, skipping")
                        continue
                else:
                    # Test for corrupt files
                    except_at_open = False
                    return_error = None
                    error_details = ""
                    try:
                        my_zip = ZipFile(os.path.join(self.settings.MEDIA_ROOT, filepath), "r")
                        return_error = my_zip.testzip()
                        my_zip.close()
                    except (BadZipFile, NotImplementedError, zlib.error) as e:
                        except_at_open = True
                        error_details = f"{type(e).__name__}: {e}"
                    if return_error:
                        error_details = f"Corrupt file: {return_error}"
                    if except_at_open or return_error:
                        logger.warning(
                            "File check on zipfile failed ({}) on file: {}, marking as corrupt.".format(
                                error_details or "unknown error", filepath
                            )
                        )
                        filesize, filecount, other_file_datas = get_zip_fileinfo(
                            os.path.join(self.settings.MEDIA_ROOT, filepath), get_extra_data=True
                        )
                        values = {
                            "title": title,
                            "title_jpn": "",
                            "zipped": filepath,
                            "crc32": crc32,
                            "match_type": "corrupt",
                            "filesize": filesize,
                            "filecount": filecount,
                            "source_type": "folder",
                            "origin": Archive.ORIGIN_FOLDER_SCAN,
                        }
                        if self.settings.archive_reason:
                            values.update({"reason": self.settings.archive_reason})
                        if self.settings.archive_details:
                            values.update({"details": self.settings.archive_details})
                        if self.settings.archive_source:
                            values.update({"source_type": self.settings.archive_source})
                        resulting_archive = Archive.objects.update_or_create_by_values_and_gid(
                            values, None, zipped=filepath, skip_zip_check=True
                        )
                        resulting_archive.fill_other_file_data(other_file_datas)
                        if self.settings.mark_similar_new_archives:
                            resulting_archive.create_marks_for_similar_archives()
                        continue

                    # Look for previous matches
                    archive_to_process = Archive.objects.filter(crc32=crc32).first()
                    if archive_to_process:
                        if self.settings.copy_match_file:
                            logger.info("Found previous match by CRC32, copying its values")
                            filesize, filecount, other_file_datas = get_zip_fileinfo(
                                os.path.join(self.settings.MEDIA_ROOT, filepath), get_extra_data=True
                            )
                            values = {
                                "title": archive_to_process.title,
                                "title_jpn": archive_to_process.title_jpn,
                                "zipped": filepath,
                                "crc32": crc32,
                                "match_type": archive_to_process.match_type,
                                "filesize": filesize,
                                "filecount": filecount,
                                "gallery_id": archive_to_process.gallery_id,
                                "source_type": archive_to_process.source_type,
                                "origin": Archive.ORIGIN_FOLDER_SCAN,
                            }
                            if self.settings.archive_reason:
                                values.update({"reason": self.settings.archive_reason})
                            if self.settings.archive_details:
                                values.update({"details": self.settings.archive_details})
                            if self.settings.archive_source:
                                values.update({"source_type": self.settings.archive_source})
                            resulting_archive = Archive.objects.add_or_update_from_values(
                                values, zipped=filepath, skip_zip_check=True
                            )
                            resulting_archive.fill_other_file_data(other_file_datas)
                            if self.settings.mark_similar_new_archives:
                                resulting_archive.create_marks_for_similar_archives()
                            continue
                        else:
                            logger.info("Matching independently and ignoring previous match")

                start_time = time.perf_counter()

                target_filesize: int | None = None
                if match_mode == MatchingMode.OFFLINE:
                    target_filesize = get_zip_fileinfo(os.path.join(self.settings.MEDIA_ROOT, filepath))[0]
                target = MatchTarget(
                    file_path=filepath,
                    crc32=crc32,
                    filesize=target_filesize,
                    # archive_to_process may be another archive with the same CRC32 at this point.
                    archive_id=Archive.objects.filter(zipped=filepath).values_list("pk", flat=True).first(),
                )
                outcome = engine.process(target)
                match_result = False
                best = outcome.best
                if best is not None:
                    match_result = engine.link_file(filepath, crc32, best) is not None

                end_time = time.perf_counter()

                logger.info("Time taken to match file {}: {:.2f} seconds.".format(filepath, (end_time - start_time)))

                if not match_result and not do_not_replace:
                    logger.info("Could not match with any matcher, adding as non-match.")
                    filesize, filecount, other_file_datas = get_zip_fileinfo(
                        os.path.join(self.settings.MEDIA_ROOT, filepath), get_extra_data=True
                    )
                    values = {
                        "title": title,
                        "title_jpn": "",
                        "zipped": filepath,
                        "crc32": crc32,
                        "match_type": "non-match",
                        "filesize": filesize,
                        "filecount": filecount,
                        "source_type": "folder",
                        "origin": Archive.ORIGIN_FOLDER_SCAN,
                    }
                    if self.settings.archive_reason:
                        values.update({"reason": self.settings.archive_reason})
                    if self.settings.archive_details:
                        values.update({"details": self.settings.archive_details})
                    if self.settings.archive_source:
                        values.update({"source_type": self.settings.archive_source})
                    archive = Archive.objects.update_or_create_by_values_and_gid(
                        values, None, zipped=filepath, skip_zip_check=True
                    )

                    archive.fill_other_file_data(other_file_datas)
                    if self.settings.mark_similar_new_archives:
                        archive.create_marks_for_similar_archives()

                    if self.settings.internal_matches_for_non_matches:
                        logger.info("Generating possible internal matches.")
                        if match_mode == MatchingMode.OFFLINE:
                            engine.save_possible_matches(archive, outcome.candidates)
                        else:
                            if internal_engine is None:
                                internal_engine = MatchingEngine(
                                    self.settings,
                                    MatchingOptions(mode=MatchingMode.OFFLINE, cutoff=0.4, clean_title=True),
                                )
                            internal_engine.match_archive(archive)
                        logger.info(
                            "Generated matches for {}, found {}".format(
                                archive.zipped.path, archive.possible_matches.count()
                            )
                        )
                elif match_result and best is not None:
                    result_message = (
                        "Matched title: {}\n"
                        "Matched link: {}\n"
                        "Matched type: {}\n"
                        "Matcher: {}\n"
                        "Match count: {}\n".format(
                            best.title, best.link, best.found_by, best.matcher, len(outcome.candidates)
                        )
                    )
                    logger.info(result_message)

            logger.info("Folder crawler matching ({}): {}".format(match_mode, engine.report.summary()))

        logger.info("Folder crawler done.")
