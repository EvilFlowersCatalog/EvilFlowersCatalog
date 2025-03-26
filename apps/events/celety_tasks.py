from evil_flowers_catalog.celery import app


@app.task
def process_task(event, payload):
    print(f"Processing event {event} with payload: {payload}")
