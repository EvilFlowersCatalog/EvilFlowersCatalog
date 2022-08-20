from .base import *

TIME_ZONE = 'Europe/Bratislava'

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '').split(';')

LOGGING['root']['level'] = 'INFO'
