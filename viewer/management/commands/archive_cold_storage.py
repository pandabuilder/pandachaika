import datetime
import typing
import uuid

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q, QuerySet
from django.utils import timezone as django_tz

from core.base.s3 import S3Error
from viewer.models import Archive
from viewer.services import cold_storage
from viewer.services.cold_storage import ColdStorageError, ColdStorageService, TransferResult
from viewer.utils.actions import event_log


def _gib(size: int | float) -> str:
    if size >= 1024**3:
        return "{:.2f} GiB".format(size / 1024**3)
    return "{:.1f} MiB".format(size / 1024**2)


class Command(BaseCommand):
    help = (
        "Archive cold tier in S3-compatible object storage: offload archives to the bucket, restore them, check that "
        "the offloaded ones are still there, or show the status. See docs/COLD_STORAGE.md."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        subparsers = parser.add_subparsers(dest="action", required=True)

        offload = subparsers.add_parser("offload", help="Upload archives to the bucket and delete the local files.")
        self._add_selection_arguments(offload)
        offload.add_argument(
            "--older-than-days",
            type=int,
            help="Only archives added more than this many days ago.",
        )
        offload.add_argument(
            "--path-prefix",
            action="append",
            default=[],
            help="Only archives whose file name (relative to MEDIA_ROOT) starts with this, e.g. galleries/hath_dls/. "
            "Can be repeated.",
        )
        offload.add_argument(
            "--exclude-path-prefix",
            action="append",
            default=[],
            help="Skip archives whose file name starts with this. Can be repeated.",
        )
        offload.add_argument(
            "--min-size-mb",
            type=float,
            help="Only archives whose (uncompressed images) size is at least this many MiB.",
        )
        offload.add_argument("--only-private", action="store_true", help="Only archives that aren't public.")
        offload.add_argument(
            "--order",
            choices=("oldest", "largest"),
            default="oldest",
            help="Which archives go first: the oldest added (default) or the largest.",
        )
        offload.add_argument(
            "--max-total-gb",
            type=float,
            help="Stop once this many GiB were offloaded.",
        )
        offload.add_argument(
            "--keep-local",
            action="store_true",
            help="Don't delete the local files (the archives still point at the bucket).",
        )

        restore = subparsers.add_parser(
            "restore", help="Download offloaded archives back to their original path and delete them from the bucket."
        )
        self._add_selection_arguments(restore)
        restore.add_argument("--all", action="store_true", help="Restore every offloaded archive.")
        restore.add_argument(
            "--keep-remote", action="store_true", help="Don't delete the objects from the bucket after restoring."
        )

        check = subparsers.add_parser(
            "check", help="Check that the offloaded archives are in the bucket (HEAD request each)."
        )
        check.add_argument("--pk", type=int, nargs="+", default=[], help="Only these archive ids.")
        check.add_argument("--limit", type=int, help="Check at most this many archives.")

        status = subparsers.add_parser("status", help="Show the configuration and how many archives are offloaded.")
        # Not "--test": pandabackup/settings.py treats any argument containing "test" as a test run.
        status.add_argument(
            "--verify",
            action="store_true",
            help="Write, read, presign and delete a small test object to check the bucket and credentials.",
        )

    @staticmethod
    def _add_selection_arguments(parser: CommandParser) -> None:
        parser.add_argument("--pk", type=int, nargs="+", default=[], help="Only these archive ids.")
        parser.add_argument(
            "--pk-file",
            help="File with archive ids (one per line, '#' comments allowed), e.g. built from the access logs.",
        )
        parser.add_argument("--limit", type=int, help="Process at most this many archives.")
        parser.add_argument("-n", "--dry-run", action="store_true", help="Only list what would be done.")
        parser.add_argument("--stop-on-error", action="store_true", help="Stop at the first archive that fails.")

    def handle(self, *args: typing.Any, **options: typing.Any) -> None:
        action = options["action"]
        if action == "status":
            self.handle_status(options)
            return
        try:
            cold_storage.get_client()
        except (ColdStorageError, ValueError) as e:
            raise CommandError(str(e))
        if action == "offload":
            self.handle_offload(options)
        elif action == "restore":
            self.handle_restore(options)
        elif action == "check":
            self.handle_check(options)

    # ------------------------------------------------------------------ selection

    @staticmethod
    def _pks(options: dict[str, typing.Any]) -> list[int]:
        pks = list(options.get("pk") or [])
        if options.get("pk_file"):
            try:
                with open(options["pk_file"], "r", encoding="utf-8") as f:
                    for line in f:
                        value = line.split("#", 1)[0].strip()
                        if value:
                            pks.append(int(value))
            except (OSError, ValueError) as e:
                raise CommandError("Could not read --pk-file: {}".format(e))
        return pks

    @staticmethod
    def _cold_filter() -> Q:
        return cold_storage.cold_name_q()

    def _offload_queryset(self, options: dict[str, typing.Any]) -> QuerySet[Archive]:
        qs = cold_storage.offloadable(Archive.objects.all())
        pks = self._pks(options)
        if pks:
            qs = qs.filter(pk__in=pks)
        if options.get("older_than_days") is not None:
            qs = qs.filter(create_date__lt=django_tz.now() - datetime.timedelta(days=options["older_than_days"]))
        if options.get("path_prefix"):
            prefix_q = Q()
            for prefix in options["path_prefix"]:
                prefix_q |= Q(zipped__startswith=prefix)
            qs = qs.filter(prefix_q)
        for prefix in options.get("exclude_path_prefix") or []:
            qs = qs.exclude(zipped__startswith=prefix)
        if options.get("min_size_mb") is not None:
            qs = qs.filter(filesize__gte=int(options["min_size_mb"] * 1024 * 1024))
        if options.get("only_private"):
            qs = qs.filter(public=False)
        if options.get("order") == "largest":
            qs = qs.order_by("-filesize", "pk")
        else:
            qs = qs.order_by("create_date", "pk")
        return qs

    # ------------------------------------------------------------------ actions

    def _report(self, verb: str, dry_verb: str, result: TransferResult, dry_run: bool) -> None:
        if result.skipped:
            self.stdout.write("  #{} skipped: {} ({})".format(result.archive_id, result.message, result.old_name))
            return
        prefix = "Would {}".format(dry_verb) if dry_run else verb
        self.stdout.write(
            "  #{} {} {} -> {} ({})".format(result.archive_id, prefix, result.old_name, result.new_name, _gib(result.size))
        )

    def _run(
        self,
        qs: QuerySet[Archive],
        verb: str,
        dry_verb: str,
        event_action: str,
        transfer: typing.Callable[[Archive], TransferResult],
        options: dict[str, typing.Any],
        max_bytes: int | None = None,
    ) -> None:
        dry_run = options["dry_run"]
        limit = options.get("limit")
        done = skipped = failed = 0
        total_bytes = 0
        # Ids first: a transfer can take minutes, don't keep a cursor open meanwhile.
        for pk in list(qs.values_list("pk", flat=True)):
            archive = Archive.objects.filter(pk=pk).first()
            if archive is None:
                continue
            if limit is not None and done >= limit:
                break
            if max_bytes is not None and total_bytes >= max_bytes:
                self.stdout.write("Reached --max-total-gb, stopping.")
                break
            try:
                result = transfer(archive)
            except (ColdStorageError, S3Error, OSError) as e:
                failed += 1
                self.stderr.write(self.style.ERROR("  #{} failed: {}".format(archive.pk, e)))
                if options.get("stop_on_error"):
                    break
                continue
            self._report(verb, dry_verb, result, dry_run)
            if result.skipped:
                skipped += 1
                continue
            done += 1
            total_bytes += result.size
            if not dry_run:
                event_log(
                    None,
                    event_action,
                    content_object=archive,
                    data="{} -> {}".format(result.old_name, result.new_name),
                    result=result.message or "success",
                )
        summary = "{}{}: {} archives ({}), {} skipped, {} failed.".format(
            "Dry run, " if dry_run else "", verb, done, _gib(total_bytes), skipped, failed
        )
        self.stdout.write(self.style.SUCCESS(summary) if not failed else self.style.WARNING(summary))

    def handle_offload(self, options: dict[str, typing.Any]) -> None:
        qs = self._offload_queryset(options)
        self.stdout.write("{} archives match.".format(qs.count()))
        max_total_gb = options.get("max_total_gb")
        self._run(
            qs,
            "Offloaded",
            "offload",
            "COLD_STORAGE_OFFLOAD",
            lambda archive: ColdStorageService.offload(
                archive, keep_local=options["keep_local"], dry_run=options["dry_run"]
            ),
            options,
            max_bytes=int(max_total_gb * 1024**3) if max_total_gb is not None else None,
        )

    def handle_restore(self, options: dict[str, typing.Any]) -> None:
        pks = self._pks(options)
        if not pks and not options["all"]:
            raise CommandError("Pass --pk, --pk-file or --all.")
        qs = Archive.objects.filter(self._cold_filter())
        if pks:
            qs = qs.filter(pk__in=pks)
        qs = qs.order_by("pk")
        self.stdout.write("{} offloaded archives match.".format(qs.count()))
        self._run(
            qs,
            "Restored",
            "restore",
            "COLD_STORAGE_RESTORE",
            lambda archive: ColdStorageService.restore(
                archive, keep_remote=options["keep_remote"], dry_run=options["dry_run"]
            ),
            options,
        )

    def handle_check(self, options: dict[str, typing.Any]) -> None:
        qs = Archive.objects.filter(self._cold_filter()).filter(file_deleted=False).order_by("pk")
        if options["pk"]:
            qs = qs.filter(pk__in=options["pk"])
        if options.get("limit") is not None:
            qs = qs[: options["limit"]]
        ok = missing = errors = 0
        total_bytes = 0
        for archive in qs.iterator():
            try:
                info = ColdStorageService.remote_info(archive)
            except (ColdStorageError, S3Error) as e:
                errors += 1
                self.stderr.write(self.style.ERROR("  #{} error: {}".format(archive.pk, e)))
                continue
            if info is None:
                missing += 1
                self.stderr.write(
                    self.style.ERROR(
                        "  #{} missing from the bucket: {}".format(
                            archive.pk, cold_storage.object_key_for(archive.zipped.name)
                        )
                    )
                )
                continue
            ok += 1
            total_bytes += info.size
        summary = "{} in the bucket ({}), {} missing, {} errors.".format(ok, _gib(total_bytes), missing, errors)
        self.stdout.write(self.style.SUCCESS(summary) if not (missing or errors) else self.style.WARNING(summary))

    def handle_status(self, options: dict[str, typing.Any]) -> None:
        conf = cold_storage.cold_settings()
        self.stdout.write("Enabled:            {}".format(conf.enable))
        self.stdout.write("Configured:         {}".format(conf.is_configured()))
        self.stdout.write("Endpoint:           {}".format(conf.endpoint_url or "-"))
        self.stdout.write("Bucket:             {} (region {})".format(conf.bucket or "-", conf.region))
        self.stdout.write("Key prefix:         {}".format(conf.key_prefix or "(bucket root)"))
        self.stdout.write("Local prefix:       {}/".format(conf.local_prefix))
        self.stdout.write("Redirect downloads: {}".format(conf.redirect_downloads))
        self.stdout.write("Presigned expiry:   {}s".format(conf.presigned_url_expiry))

        live = Archive.objects.filter(file_deleted=False).exclude(zipped="")
        cold = live.filter(self._cold_filter())
        self.stdout.write(
            "Offloaded archives: {} of {} with a file".format(cold.count(), live.count())
        )

        if not options["verify"]:
            return
        try:
            client = cold_storage.get_client()
        except (ColdStorageError, ValueError) as e:
            raise CommandError(str(e))
        key = "{}.pandagallery-cold-storage-test-{}".format(conf.key_prefix, uuid.uuid4().hex)
        payload = b"pandagallery cold storage test\n"
        try:
            client.put_object(key, payload, content_type="text/plain")
            info = client.head_object(key)
            if info is None or info.size != len(payload):
                raise CommandError("Test object {} was written but HEAD returned {}".format(key, info))
            url = client.presigned_get_url(key, expires=60)
            import requests

            response = requests.get(url, timeout=conf.timeout)
            if response.status_code != 200 or response.content != payload:
                raise CommandError(
                    "Presigned GET of the test object returned HTTP {}: {}".format(
                        response.status_code, response.text[:300]
                    )
                )
        except S3Error as e:
            raise CommandError("Bucket test failed: {}".format(e))
        finally:
            try:
                client.delete_object(key)
            except S3Error as e:
                self.stderr.write("Could not delete the test object {}: {}".format(key, e))
        self.stdout.write(self.style.SUCCESS("Bucket test passed: write, HEAD, presigned GET and delete work."))
