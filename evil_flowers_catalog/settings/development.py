from .base import *

DEBUG = True

TIME_ZONE = 'Europe/Bratislava'

ALLOWED_HOSTS = [
    '*'
]

EMAIL_BACKEND = 'django_imap_backend.ImapBackend'
EMAIL_IMAP_SECRETS = [
    {
        'HOST': os.getenv('EMAIL_IMAP_HOST'),
        'USER': os.getenv('EMAIL_IMAP_USER'),
        'PASSWORD': os.getenv('EMAIL_IMAP_PASSWORD'),
        'MAILBOX': os.getenv('EMAIL_IMAP_MAILBOX'),
        'SSL': False
    }
]
