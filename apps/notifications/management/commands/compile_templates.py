from django.core.management.base import BaseCommand

from apps.notifications.registry import TemplateRegistry


class Command(BaseCommand):
    help = "Validate all notification MJML templates by compiling them"

    def handle(self, *args, **options):
        template_names = TemplateRegistry.get_template_names()

        if not template_names:
            self.stdout.write(self.style.WARNING("No notification templates found."))
            return

        self.stdout.write(f"Found {len(template_names)} notification template(s): {', '.join(template_names)}")

        errors = TemplateRegistry.validate_templates()

        if not errors:
            self.stdout.write(self.style.SUCCESS("All templates are valid."))
        else:
            for name, template_errors in errors.items():
                self.stdout.write(self.style.ERROR(f"\n{name}:"))
                for error in template_errors:
                    self.stdout.write(f"  - {error}")
