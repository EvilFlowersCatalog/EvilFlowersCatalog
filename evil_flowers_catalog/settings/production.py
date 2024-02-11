from .base import *

TIME_ZONE = "Europe/Bratislava"

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "").split(";") + ["localhost"]

SECURE_SSL_REDIRECT = True

LOGGING["root"]["level"] = "INFO"
