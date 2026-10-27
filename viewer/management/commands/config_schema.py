import json
import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError
import yaml

from core.base.config_models import (
    PandaGalleryConfig,
    generate_default_yaml_content,
    get_config_schema,
    save_default_yaml_file,
)


class Command(BaseCommand):
    help = "Manage, inspect, and export Panda Backup configuration schemas and default YAML."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output the configuration JSON Schema (definitions and types) to stdout.",
        )
        parser.add_argument(
            "--yaml",
            action="store_true",
            help="Output the documented default YAML configuration to stdout.",
        )
        parser.add_argument(
            "--output",
            type=str,
            help="File path to save the generated JSON schema or YAML output.",
        )
        parser.add_argument(
            "--update-default",
            action="store_true",
            help="Regenerate and update the repository default.yaml file with comprehensive documentation.",
        )
        parser.add_argument(
            "--validate",
            type=str,
            nargs="?",
            const="default.yaml",
            help="Validate a YAML configuration file against the Pydantic configuration schema.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options.get("update_default"):
            target_path = options.get("output") or "default.yaml"
            save_default_yaml_file(target_path)
            self.stdout.write(self.style.SUCCESS(f"Successfully updated default configuration file: {target_path}"))
            return

        if options.get("validate"):
            file_path = options["validate"]
            if not os.path.isfile(file_path):
                raise CommandError(f"Configuration file not found: {file_path}")
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                PandaGalleryConfig.model_validate(data)
                self.stdout.write(self.style.SUCCESS(f"Configuration file '{file_path}' is valid!"))
            except Exception as e:
                raise CommandError(f"Configuration validation failed for '{file_path}': {e}")
            return

        if options.get("json"):
            schema = get_config_schema()
            output_str = json.dumps(schema, indent=2)
            if options.get("output"):
                with open(options["output"], "w", encoding="utf-8") as f:
                    f.write(output_str)
                self.stdout.write(self.style.SUCCESS(f"Saved configuration JSON schema to {options['output']}"))
            else:
                self.stdout.write(output_str)
            return

        if options.get("yaml"):
            yaml_content = generate_default_yaml_content()
            if options.get("output"):
                with open(options["output"], "w", encoding="utf-8") as f:
                    f.write(yaml_content)
                self.stdout.write(self.style.SUCCESS(f"Saved default YAML to {options['output']}"))
            else:
                self.stdout.write(yaml_content)
            return

        # Default behavior if no flag passed: print help
        schema = get_config_schema()
        output_str = json.dumps(schema, indent=2)
        if options.get("output"):
            with open(options["output"], "w", encoding="utf-8") as f:
                f.write(output_str)
            self.stdout.write(self.style.SUCCESS(f"Saved configuration JSON schema to {options['output']}"))
        else:
            self.stdout.write(output_str)
