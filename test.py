from config.Config import Config

from celery import Celery

app = Celery('text_service', broker='redis://redis:6379/0', backend=Config.REDIS_URL  # Already there, but verify
)

result = app.send_task(
    'evilflowers_text_worker.process_pdf',
    args=['algebra-a-diskretna-matematika.pdf', 'test-123'],
    queue='evilflowers_text_worker'
)

print(f"Task ID: {result.id}")
print(result.get(timeout=300))  # Wait up to 5 minutes