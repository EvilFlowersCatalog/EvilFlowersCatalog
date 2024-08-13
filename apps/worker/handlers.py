from apps.worker.models import JobProtocol


def exception_handler(job, exc_type, exc_value, traceback):
    try:
        protocol = JobProtocol.objects.get(pk=job.get_id())
        protocol.recount_status = JobProtocol.JobStatus.FAILED
        protocol.save()
    except JobProtocol.DoesNotExist:
        pass
