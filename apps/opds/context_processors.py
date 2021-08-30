from django.conf import settings


def basic_settings(request) -> dict:
    return {
        'BASIC_SETTINGS': {
            'BASE_URL': settings.BASE_URL,
            'INSTANCE_NAME': settings.INSTANCE_NAME
        }
    }
