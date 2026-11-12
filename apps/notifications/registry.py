import os

from django.template.loader import get_template


class TemplateRegistry:
    """Discovers and validates notification templates."""

    TEMPLATE_DIR = "notifications"
    REQUIRED_EXTENSIONS = (".mjml", ".txt")

    @classmethod
    def sample_context(cls, user_name: str = "Alena Nováková") -> dict:
        """One context that fills every variable any notification template uses — for previews (IP-025).

        Times are relative to now so "expires in N days" reads plausibly.
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        return {
            "user_name": user_name,
            "entry_title": "Úvod do teórie grafov",
            "entry_author": "Ján Novák",
            "starts_at": now.isoformat(),
            "expires_at": (now + timedelta(days=14)).isoformat(),
            "days_left": 3,
            "passphrase_hint": "meno môjho prvého psa",
            "download_url": "https://example.invalid/readium/v1/licenses/preview.lcpl?access_token=preview",
            "download_expires_hours": 72,
            "claim_url": "https://example.invalid/library/reservations/preview/claim",
            "claim_deadline": (now + timedelta(hours=48)).isoformat(),
            "claim_window_hours": 48,
            "position": 2,
            "old_position": 3,
            "new_position": 2,
            "estimated_available_from": (now + timedelta(days=5)).isoformat(),
            "estimated_available_until": (now + timedelta(days=12)).isoformat(),
            "reason": "Ukážkový dôvod / sample reason",
        }

    @classmethod
    def get_template_names(cls) -> list[str]:
        """Return list of notification type names that have all required templates."""
        from django.conf import settings

        template_names = set()
        for engine in settings.TEMPLATES:
            for template_dir in engine.get("DIRS", []):
                notifications_dir = os.path.join(template_dir, cls.TEMPLATE_DIR)
                if os.path.isdir(notifications_dir):
                    for filename in os.listdir(notifications_dir):
                        name, ext = os.path.splitext(filename)
                        if ext == ".mjml" and name != "base":
                            template_names.add(name)

        # Also check app template dirs
        for app_config in settings.INSTALLED_APPS:
            try:
                app_path = __import__(app_config, fromlist=[""]).__path__[0]
                tpl_dir = os.path.join(app_path, "templates", cls.TEMPLATE_DIR)
                if os.path.isdir(tpl_dir):
                    for filename in os.listdir(tpl_dir):
                        name, ext = os.path.splitext(filename)
                        if ext == ".mjml" and name != "base":
                            template_names.add(name)
            except (ImportError, AttributeError, IndexError):
                continue

        return sorted(template_names)

    @classmethod
    def validate_templates(cls) -> dict[str, list[str]]:
        """Validate all notification templates. Returns dict of errors per template name."""
        from mjml import mjml2html

        errors: dict[str, list[str]] = {}

        for name in cls.get_template_names():
            template_errors = []

            # Check MJML template exists and compiles
            try:
                tpl = get_template(f"{cls.TEMPLATE_DIR}/{name}.mjml")
                rendered = tpl.render({})
                mjml2html(rendered)
            except Exception as e:
                template_errors.append(f"MJML template error: {e}")

            # Check plain-text template exists
            try:
                get_template(f"{cls.TEMPLATE_DIR}/{name}.txt")
            except Exception as e:
                template_errors.append(f"Plain-text template missing: {e}")

            # Check subject template exists
            try:
                get_template(f"{cls.TEMPLATE_DIR}/subjects/{name}.txt")
            except Exception as e:
                template_errors.append(f"Subject template missing: {e}")

            if template_errors:
                errors[name] = template_errors

        return errors
