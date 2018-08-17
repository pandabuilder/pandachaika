import django.dispatch
from django.utils import timezone as django_tz

from viewer.models import EventLog


def event_log(user, action, reason=None, data=None, result=None, content_object=None, create_date=None):
    if user is not None and not user.is_authenticated:
        user = None
    if create_date is None:
        create_date = django_tz.now()

    event = EventLog.objects.create(
        user=user,
        action=action,
        reason=reason,
        data=data,
        result=result,
        content_object=content_object,
        create_date=create_date
    )
    django.dispatch.Signal(providing_args=["event"]).send(sender=EventLog, event=event)
    return event
