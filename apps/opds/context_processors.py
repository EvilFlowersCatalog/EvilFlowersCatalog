from django.conf import settings


def basic_settings(request) -> dict:
    return {
        'BASIC_SETTINGS': {
            'BASE_URL': settings.BASE_URL,
            'DEBUG': settings.DEBUG,
            'VERSION': settings.VERSION,
            'INSTANCE_NAME': settings.INSTANCE_NAME,
            'CONTACT_EMAIL': settings.EVILFLOWERS_CONTACT_EMAIL
        }
    }
