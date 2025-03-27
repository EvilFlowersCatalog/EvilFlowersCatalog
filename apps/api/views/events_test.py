from django.http import HttpResponse

from apps.events.services import event_servise


def event_test(request):
    event_servise.execute("test", {"data": "test"})
    return HttpResponse("OK")
