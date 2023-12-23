from .base import *

TIME_ZONE = "Europe/Bratislava"

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "").split(";") + ["localhost"]

LOGGING["root"]["level"] = "INFO"
