import factory
from django.contrib.auth import get_user_model
from workers.models import SchedulerState, TaskQueue, WorkerProcess

User = get_user_model()


class TaskQueueFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TaskQueue

    task_type = "web_crawler"
    args = factory.LazyFunction(lambda: ["https://example.com/test"])
    options = factory.LazyFunction(dict)
    priority = 10
    status = TaskQueue.Status.PENDING


class SchedulerStateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SchedulerState

    name = factory.Sequence(lambda n: f"scheduler_{n}")
    is_running = False
    is_paused = False


class WorkerProcessFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WorkerProcess

    pid = factory.Sequence(lambda n: 10000 + n)
    status = "idle"
