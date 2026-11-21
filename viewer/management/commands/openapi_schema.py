from typing import Any

from django.core.management.base import BaseCommand, CommandError

from viewer.views.api.openapi import STATIC_DOCUMENT_PATH, STATIC_DOCUMENT_SERVERS, render_openapi_document


class Command(BaseCommand):
    help = (
        "Export the OpenAPI document of /api/v1/ (the one served at /api/v1/openapi.json). "
        f"--update rewrites the copy in the repository ({STATIC_DOCUMENT_PATH.relative_to(STATIC_DOCUMENT_PATH.parents[2])}), "
        "which GitLab renders with Swagger UI; --check fails if that copy is out of date."
    )

    def add_arguments(self, parser: Any) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--update", action="store_true", help="Rewrite the document committed to the repository.")
        mode.add_argument("--check", action="store_true", help="Exit with an error if the committed document differs.")
        mode.add_argument("--output", type=str, help="Write the document to this file instead of stdout.")
        parser.add_argument(
            "--server",
            action="append",
            dest="servers",
            metavar="URL",
            help="Server URL to list in the document (repeatable). Default: relative to the host serving it; for "
            f"--update and --check: {', '.join(server['url'] for server in STATIC_DOCUMENT_SERVERS)}.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        servers: list[dict[str, str]] | None = (
            [{"url": url} for url in options["servers"]] if options["servers"] else None
        )
        if options["update"] or options["check"]:
            content = render_openapi_document(servers if servers is not None else STATIC_DOCUMENT_SERVERS)
            if options["check"]:
                current = STATIC_DOCUMENT_PATH.read_text(encoding="utf-8") if STATIC_DOCUMENT_PATH.exists() else ""
                if current != content:
                    raise CommandError(
                        f"{STATIC_DOCUMENT_PATH} is out of date. Run: python manage.py openapi_schema --update"
                    )
                self.stdout.write(f"{STATIC_DOCUMENT_PATH} is up to date.")
                return
            STATIC_DOCUMENT_PATH.write_text(content, encoding="utf-8")
            self.stdout.write(f"Wrote {STATIC_DOCUMENT_PATH}")
            return

        content = render_openapi_document(servers)
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as f:
                f.write(content)
            self.stdout.write(f"Wrote {options['output']}")
        else:
            self.stdout.write(content, ending="")
