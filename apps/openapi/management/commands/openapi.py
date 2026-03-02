import json
import os
from pathlib import Path

from django.core.management import BaseCommand
from django.conf import settings

from apps.openapi.services import OpenApiService


class Command(BaseCommand):
    help = "Generate OpenAPI specification"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            "-o",
            type=str,
            help="Output file path (default: stdout)",
        )
        parser.add_argument(
            "--format",
            "-f",
            type=str,
            choices=["json", "yaml"],
            default="json",
            help="Output format (default: json)",
        )
        parser.add_argument(
            "--indent",
            type=int,
            default=2,
            help="JSON indentation level (default: 2)",
        )
        parser.add_argument(
            "--validate",
            action="store_true",
            help="Validate the generated OpenAPI specification",
        )
        parser.add_argument(
            "--stats",
            action="store_true",
            help="Show statistics about the generated specification",
        )

    def handle(self, *args, **options):
        service = OpenApiService()

        try:
            spec = service.build()

            if options["stats"]:
                self.show_stats(spec)

            if options["validate"]:
                self.validate_spec(spec)

            # Format output
            if options["format"] == "yaml":
                try:
                    import yaml

                    output = yaml.dump(spec, default_flow_style=False)
                except ImportError:
                    self.stdout.write(
                        self.style.ERROR("PyYAML is required for YAML output. Install it with: pip install PyYAML")
                    )
                    return
            else:
                output = json.dumps(spec, indent=options["indent"])

            # Write to file or stdout
            if options["output"]:
                output_path = Path(options["output"])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "w") as f:
                    f.write(output)
                self.stdout.write(self.style.SUCCESS(f"OpenAPI specification written to {output_path}"))
            else:
                self.stdout.write(output)

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error generating OpenAPI specification: {e}"))
            raise

    def show_stats(self, spec):
        """Show statistics about the generated specification."""
        paths = spec.get("paths", {})
        components = spec.get("components", {})
        schemas = components.get("schemas", {})

        operations = []
        for path, methods in paths.items():
            for method, operation in methods.items():
                if method != "parameters":  # Skip parameters key
                    operations.append(f"{method.upper()} {path}")

        self.stdout.write(self.style.SUCCESS("OpenAPI Specification Statistics:"))
        self.stdout.write(f"  Paths: {len(paths)}")
        self.stdout.write(f"  Operations: {len(operations)}")
        self.stdout.write(f"  Schemas: {len(schemas)}")
        self.stdout.write(f"  Security Schemes: {len(components.get('securitySchemes', {}))}")
        self.stdout.write("")

    def validate_spec(self, spec):
        """Validate the OpenAPI specification."""
        try:
            # Basic validation
            required_fields = ["openapi", "info", "paths"]
            for field in required_fields:
                if field not in spec:
                    self.stdout.write(self.style.ERROR(f"Missing required field: {field}"))
                    return False

            # Check OpenAPI version
            if not spec["openapi"].startswith("3."):
                self.stdout.write(self.style.WARNING(f"OpenAPI version {spec['openapi']} may not be supported"))

            # Check for empty paths
            if not spec["paths"]:
                self.stdout.write(self.style.WARNING("No paths defined in specification"))

            self.stdout.write(self.style.SUCCESS("OpenAPI specification validation passed"))
            return True

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Validation error: {e}"))
            return False
