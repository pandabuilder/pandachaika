# -*- coding: utf-8 -*-
import fnmatch
import logging
import os
import re
import time
import argparse
from typing import Union, NoReturn
from zipfile import BadZipFile
from zipfile import ZipFile

from core.base.comparison import get_closer_gallery_title_from_list
from core.base.setup import Settings
from core.base.types import DataDict
from core.base.utilities import (
    calc_crc32, get_zip_fileinfo,
    replace_illegal_name
)

from viewer.models import Archive, Gallery

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

    def get_args(self, arg_line: list[str]) -> Union[argparse.Namespace, ArgumentParserError]:

        parser = YieldingArgumentParser(prog='PandaBackupFolder')

        parser.add_argument('folder',
                            # action='append',
                            nargs='*',
                            default=[],
                            help='folders to crawl. Must be relative to the media_url')

        parser.add_argument('-reason', '--set-reason',
                            required=False,
                            action='store',
                            help='Most parameters are set from the gallery, but reason is user-defined.'
                                 'Can be set afterwards, but this option sets it when crawling. For new files only')

        parser.add_argument('-source', '--set-source',
                            required=False,
                            action='store',
                            help='Override the default source_type for '
                                 'archives added from the filesystem (default: folder)')

        parser.add_argument('-dmf', '--display-missing-files',
                            required=False,
                            action='store_true',
                            help='Display archives missing from the filesystem')

        parser.add_argument('-rmf', '--remove-missing-files',
                            required=False,
                            action='store_true',
                            help="Remove from the database archives missing from the filesystem")

        parser.add_argument('-frm', '--force-rematch',
                            required=False,
                            action='store_true',
                            help='Force rematch of files, disregarding the match_type')

        parser.add_argument('-rnm', '--rematch-non-matches',
                            required=False,
                            action='store_true',
                            help='Rematch archives that could not be matched')

        parser.add_argument('-rtt', '--rename-to-title',
                            required=False,
                            action='store',
                            nargs='+',
                            metavar='action/paths',
                            help=(
                                'Rename archives in the filesystem to their title. '
                                'If the first option is "rename", the rename will be effective. '
                                'Otherwise, it will just display the possible rename. '
                                'Must specify the paths to analyze. This is based on the relative path')
                            )

        parser.add_argument('-aftt', '--all-filenames-to-title',
                            required=False,
                            action='store',
                            # default='display',
                            metavar='action',
                            help=(
                                'Rename all archives that their basename doesn\'t match with the '
                                'title of the archive. If the action is "rename", the rename will be effective. '
                                'Otherwise, it will just display the possible rename')
                            )

        parser.add_argument('-dmfigt', '--display-match-from-internal-gallery-titles',
                            required=False,
                            action='store',
                            type=float,
                            metavar='cutoff',
                            help=(
                                'Try to match archives marked as non-matches with'
                                ' the local galleries database, based on the title. '
                                'Needs a cutoff value to discard likeness. '
                                'This will only display the matches, not apply them')
                            )

        parser.add_argument('-rfigt', '--rematch-from-internal-gallery-titles',
                            required=False,
                            action='store',
                            type=float,
                            metavar='cutoff',
                            help=(
                                'Try to match archives marked as non-matches '
                                'with the local galleries database, based on the title. '
                                'Needs a cutoff value to discard likeness. This will apply the changes')
                            )

        parser.add_argument('-rmc', '--recalc-missing-crc32',
                            required=False,
                            action='store_true',
                            help='Recalculate file info for archives with missing CRC32')

        parser.add_argument('-rbm', '--rematch-by-match-type',
                            required=False,
                            action='store',
                            metavar='match_type',
                            help='Rematch archives that have a certain match_type')

        parser.add_argument('-rwf', '--rematch-wrong-filesize',
                            required=False,
                            action='store_true',
                            help='Rematch archives that their filesize is '
                                 'different from the gallery filesize (unpacked)')
        try:
            args = parser.parse_args(arg_line)
            self.parse_error = False
        except ArgumentParserError as e:
            self.parse_error = True
            return e

        if args.rename_to_title:
            if args.rename_to_title[0] == 'rename':
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
                        Archive.objects.delete_by_filter(
                            pk=archive.pk)

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
            self.settings.rematch_file_list = ['non-match']
            self.settings.rematch_file = True
            found_archives = Archive.objects.filter(
                match_type='non-match')
            if found_archives:
                logger.info("Scanning {} archives with non-matches".format(found_archives.count()))
                for archive in found_archives:
                    if os.path.isfile(archive.zipped.path):
                        files.append(archive.zipped.path)

        elif args.rematch_by_match_type:
            self.settings.rematch_file_list = [args.rematch_by_match_type]
            self.settings.rematch_file = True
            self.settings.replace_metadata = True
            found_archives = Archive.objects.filter(
                match_type=args.rematch_by_match_type)
            if found_archives:
                logger.info("Scanning {} archives matched by {}".format(
                    found_archives.count(), args.rematch_by_match_type
                ))
                for archive in found_archives:
                    if os.path.isfile(archive.zipped.path):
                        files.append(archive.zipped.path)
        elif args.rematch_wrong_filesize:
            self.settings.rematch_file = True
            self.settings.replace_metadata = True
            do_not_replace = True
            found_archives = Archive.objects.exclude(
                match_type='non-match', gallery_id__isnull=True)
            if found_archives:
                for archive in found_archives:
                    if not os.path.isfile(archive.zipped.path):
                        continue
                    if not archive.gallery or archive.filesize == archive.gallery.filesize:
                        continue
                    files.append(archive.zipped.path)
                logger.info("Scanning {} archives matched with wrong filesize".format(len(files)))
        elif args.recalc_missing_crc32:

            found_archives = Archive.objects.filter(crc32='')

            if found_archives:
                logger.info("Calculating {} archives with missing CRC32".format(found_archives.count()))
                for cnt, archive in enumerate(found_archives):
                    if os.path.isfile(archive.zipped.path):
                        crc32 = calc_crc32(
                            archive.zipped.path)
                        logger.info("Working on archive {} of {}, CRC32: {}".format((cnt + 1), found_archives.count(), crc32))
                        values = {'crc32': crc32}
                        Archive.objects.add_or_update_from_values(
                            values, pk=archive.pk)
                    else:
                        logger.info("Archive {} of {}, path: {} does not exist".format(
                            (cnt + 1),
                            found_archives.count(),
                            archive.zipped.path
                        ))
            return
        elif args.all_filenames_to_title:

            archives_filename_to_title = Archive.objects.exclude(title='')

            if archives_filename_to_title:
                logger.info("Checking {} archives".format(archives_filename_to_title.count()))
                for cnt, archive in enumerate(archives_filename_to_title):
                    if not archive.title:
                        continue
                    current_path = os.path.join(os.path.dirname(
                        archive.zipped.path), replace_illegal_name(archive.title) + '.zip')

                    if archive.zipped.path != current_path and not os.path.isfile(os.path.join(self.settings.MEDIA_ROOT, current_path)):
                        logger.info("Filename should be {} but it's {}".format(current_path, archive.zipped.path))
                        if args.filename_to_title == 'rename':
                            os.rename(archive.zipped.path, os.path.join(
                                self.settings.MEDIA_ROOT, current_path))
                            values = {'zipped': current_path,
                                      }
                            Archive.objects.add_or_update_from_values(
                                values, pk=archive.pk)
            return

        elif args.rematch_from_internal_gallery_titles:

            non_matched_archives = Archive.objects.filter(
                match_type='non-match')

            if non_matched_archives:

                archives_title_gid, galleries_title_gid = self.get_archive_and_gallery_titles()

                logger.info("Matching against archive and gallery database, {} archives with no match".format(non_matched_archives.count()))
                for archive in non_matched_archives:
                    adjusted_title = replace_illegal_name(
                        os.path.basename(archive.zipped.path)).replace(".zip", "")

                    galleries_id_token = get_closer_gallery_title_from_list(
                        adjusted_title, galleries_title_gid, args.rematch_from_internal_gallery_titles)
                    if galleries_id_token is not None:
                        logger.info("Path: {}\nGal title: {}".format(adjusted_title, galleries_id_token[0]))
                        values = {
                            'title': Gallery.objects.filter(id=galleries_id_token[1])[0].title,
                            'title_jpn': Gallery.objects.filter(id=galleries_id_token[1])[0].title_jpn,
                            'zipped': archive.zipped.path,
                            'crc32': archive.crc32,
                            'match_type': 'gallery_database',
                            'filesize': archive.filesize,
                            'filecount': archive.filecount,
                            'gallery_id': galleries_id_token[1]
                        }
                        Archive.objects.add_or_update_from_values(
                            values, pk=archive.pk)
                        Gallery.objects.update_by_dl_type(
                            {"dl_type": "folder:filename"}, galleries_id_token[1], "failed")
                    else:
                        galleries_id_token = get_closer_gallery_title_from_list(
                            adjusted_title, archives_title_gid, args.rematch_from_internal_gallery_titles)
                        if galleries_id_token is not None:
                            logger.info("Path: {}\nMatch title: {}".format(adjusted_title, galleries_id_token[0]))
                            values = {
                                'title': Gallery.objects.filter(id=galleries_id_token[1])[0].title,
                                'title_jpn': Gallery.objects.filter(id=galleries_id_token[1])[0].title_jpn,
                                'zipped': archive.zipped.path,
                                'crc32': archive.crc32,
                                'match_type': archive.match_type,
                                'filesize': archive.filesize,
                                'filecount': archive.filecount,
                                'gallery_id': galleries_id_token[1]
                            }
                            Archive.objects.add_or_update_from_values(
                                values, pk=archive.pk)

            return

        elif args.display_match_from_internal_gallery_titles:

            non_matched_archives = Archive.objects.filter(
                match_type='non-match')

            if non_matched_archives:

                archives_title_gid, galleries_title_gid = self.get_archive_and_gallery_titles()

                logger.info("Matching against archive and gallery database, {} archives with no match".format(non_matched_archives.count()))
                for archive in non_matched_archives:
                    adjusted_title = replace_illegal_name(
                        os.path.basename(archive.zipped.path)).replace(".zip", "")
                    galleries_id_token = get_closer_gallery_title_from_list(
                        adjusted_title, galleries_title_gid, args.display_match_from_internal_gallery_titles)
                    if galleries_id_token is not None:
                        logger.info("Path: {}\nGal title: {}".format(adjusted_title, galleries_id_token[0]))
                    else:
                        galleries_id_token = get_closer_gallery_title_from_list(
                            adjusted_title, archives_title_gid, args.display_match_from_internal_gallery_titles)
                        if galleries_id_token is not None:
                            logger.info("Path: {}\nMatch title: {}".format(adjusted_title, galleries_id_token[0]))

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
                                files.append(
                                    os.path.relpath(os.path.join(root, filename), self.settings.MEDIA_ROOT))
                elif os.path.isfile(os.path.join(self.settings.MEDIA_ROOT, folder)):
                    files.append(folder)

        if args.rename_to_title:
            logger.info("Checking {} archives".format(len(files)))
            for cnt, filepath in enumerate(files):

                archive_to_rename = Archive.objects.filter(zipped=filepath).first()

                if archive_to_rename and archive_to_rename.title:
                    current_path = os.path.join(
                        os.path.dirname(filepath), replace_illegal_name(archive_to_rename.title) + '.zip')

                    if filepath != current_path and not os.path.isfile(os.path.join(self.settings.MEDIA_ROOT, current_path)):
                        logger.info("Filename should be {} but it's {}".format(current_path, filepath))
                        if args.rename_to_title == 'rename':
                            os.rename(os.path.join(self.settings.MEDIA_ROOT, filepath), os.path.join(
                                self.settings.MEDIA_ROOT, current_path))
                            values = {'zipped': current_path,
                                      }
                            Archive.objects.add_or_update_from_values(
                                values, zipped=filepath)
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

            matchers_list = self.settings.provider_context.get_matchers(self.settings)
            for matcher in matchers_list:
                logger.info("Using matcher {} with a priority of {}".format(matcher[0].name, matcher[1]))

            for cnt, filepath in enumerate(files):

                logger.info("Checking file: {} of {}, path: {}".format((cnt + 1), len(files), filepath))

                title = re.sub(
                    '[_]', ' ', os.path.splitext(os.path.basename(filepath))[0])
                archive_to_process = Archive.objects.filter(zipped=filepath).first()
                if not self.settings.rehash_files and archive_to_process:
                    crc32 = archive_to_process.crc32
                else:
                    crc32 = calc_crc32(
                        os.path.join(self.settings.MEDIA_ROOT, filepath))

                if archive_to_process:
                    if args.force_rematch:
                        logger.info("Doing a forced rematch")
                    elif archive_to_process.match_type in self.settings.rematch_file_list or args.rematch_wrong_filesize:
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
                    try:
                        my_zip = ZipFile(os.path.join(self.settings.MEDIA_ROOT, filepath), 'r')
                        return_error = my_zip.testzip()
                        my_zip.close()
                    except (BadZipFile, NotImplementedError):
                        except_at_open = True
                    if except_at_open or return_error:
                        logger.warning("File check on zipfile failed on file: {}, marking as corrupt.".format(filepath))
                        filesize, filecount, other_file_datas = get_zip_fileinfo(os.path.join(self.settings.MEDIA_ROOT, filepath), get_extra_data=True)
                        values = {
                            'title': title,
                            'title_jpn': '',
                            'zipped': filepath,
                            'crc32': crc32,
                            'match_type': 'corrupt',
                            'filesize': filesize,
                            'filecount': filecount,
                            'source_type': 'folder',
                            'origin': Archive.ORIGIN_FOLDER_SCAN
                        }
                        if self.settings.archive_reason:
                            values.update({'reason': self.settings.archive_reason})
                        if self.settings.archive_details:
                            values.update({'details': self.settings.archive_details})
                        if self.settings.archive_source:
                            values.update({'source_type': self.settings.archive_source})
                        resulting_archive = Archive.objects.update_or_create_by_values_and_gid(
                            values, None, zipped=filepath)
                        resulting_archive.fill_other_file_data(other_file_datas)
                        if self.settings.mark_similar_new_archives:
                            resulting_archive.create_marks_for_similar_archives()
                        continue

                    # Look for previous matches
                    archive_to_process = Archive.objects.filter(crc32=crc32).first()
                    if archive_to_process:
                        if self.settings.copy_match_file:
                            logger.info("Found previous match by CRC32, copying its values")
                            filesize, filecount, other_file_datas = get_zip_fileinfo(os.path.join(self.settings.MEDIA_ROOT, filepath), get_extra_data=True)
                            values = {
                                'title': archive_to_process.title,
                                'title_jpn': archive_to_process.title_jpn,
                                'zipped': filepath,
                                'crc32': crc32,
                                'match_type': archive_to_process.match_type,
                                'filesize': filesize,
                                'filecount': filecount,
                                'gallery_id': archive_to_process.gallery_id,
                                'source_type': archive_to_process.source_type,
                                'origin': Archive.ORIGIN_FOLDER_SCAN
                            }
                            if self.settings.archive_reason:
                                values.update({'reason': self.settings.archive_reason})
                            if self.settings.archive_details:
                                values.update({'details': self.settings.archive_details})
                            if self.settings.archive_source:
                                values.update({'source_type': self.settings.archive_source})
                            resulting_archive = Archive.objects.add_or_update_from_values(
                                values, zipped=filepath)
                            resulting_archive.fill_other_file_data(other_file_datas)
                            if self.settings.mark_similar_new_archives:
                                resulting_archive.create_marks_for_similar_archives()
                            continue
                        else:
                            logger.info("Matching independently and ignoring previous match")

                match_result = False

                start_time = time.perf_counter()

                match_type = ''
                match_title = ''
                match_link = ''
                match_count = 0

                for i, matcher in enumerate(matchers_list):
                    if i > 0:
                        current_provider = matchers_list[i][0].provider
                        # wait always in current_provider, independently of last used provider
                        if current_provider in self.settings.providers:
                            time.sleep(self.settings.providers[current_provider].wait_timer)
                        else:
                            time.sleep(self.settings.wait_timer)
                    logger.info("Matching with: {}".format(matcher[0]))
                    if matcher[0].start_match(filepath, crc32):
                        match_type = matcher[0].found_by
                        match_title = matcher[0].match_title or ''
                        match_link = matcher[0].match_link or ''
                        match_count = matcher[0].match_count
                        match_result = True
                        break

                end_time = time.perf_counter()

                logger.info("Time taken to match file {}: {:.2f} seconds.".format(filepath, (end_time - start_time)))

                if not match_result and not do_not_replace:
                    logger.info('Could not match with any matcher, adding as non-match.')
                    filesize, filecount, other_file_datas = get_zip_fileinfo(os.path.join(self.settings.MEDIA_ROOT, filepath), get_extra_data=True)
                    values = {
                        'title': title,
                        'title_jpn': '',
                        'zipped': filepath,
                        'crc32': crc32,
                        'match_type': 'non-match',
                        'filesize': filesize,
                        'filecount': filecount,
                        'source_type': 'folder',
                        'origin': Archive.ORIGIN_FOLDER_SCAN
                    }
                    if self.settings.archive_reason:
                        values.update({'reason': self.settings.archive_reason})
                    if self.settings.archive_details:
                        values.update({'details': self.settings.archive_details})
                    if self.settings.archive_source:
                        values.update({'source_type': self.settings.archive_source})
                    archive = Archive.objects.update_or_create_by_values_and_gid(
                        values, None, zipped=filepath)

                    archive.fill_other_file_data(other_file_datas)
                    if self.settings.mark_similar_new_archives:
                        archive.create_marks_for_similar_archives()

                    if self.settings.internal_matches_for_non_matches:
                        logger.info('Generating possible internal matches.')

                        archive.generate_possible_matches(cutoff=0.4, clear_title=True)
                        logger.info('Generated matches for {}, found {}'.format(
                            archive.zipped.path,
                            archive.possible_matches.count()
                        ))
                elif match_result:
                    result_message = (
                        "Matched title: {}\n"
                        "Matched link: {}\n"
                        "Matched type: {}\n"
                        "Match count: {}\n".format(match_title, match_link, match_type, match_count)
                    )
                    logger.info(result_message)

        logger.info('Folder crawler done.')

    @staticmethod
    def get_archive_and_gallery_titles() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        found_galleries = Gallery.objects.eligible_for_use().exclude(title='')
        found_archives = Archive.objects.exclude(
            match_type__in=('', 'non-match')).exclude(title='').exclude(gallery__isnull=True)
        archives_title_gid = []
        galleries_title_gid = []
        for archive in found_archives:
            if not archive.title or not archive.gallery:
                continue
            archives_title_gid.append(
                (replace_illegal_name(archive.title), str(archive.gallery.id)))
        for gallery in found_galleries:
            if not gallery.title:
                continue
            if 'replaced' in gallery.tag_list():
                continue
            galleries_title_gid.append(
                (replace_illegal_name(gallery.title), str(gallery.id)))
        return archives_title_gid, galleries_title_gid
