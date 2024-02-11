from .base import *

TIME_ZONE = "Europe/Bratislava"

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "").split(";") + ["localhost"]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING["root"]["level"] = "INFO"
