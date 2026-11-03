from datetime import datetime, timezone
import factory
from django.contrib.auth import get_user_model
from viewer.models import Archive, Gallery, Provider, Tag, WantedGallery

User = get_user_model()


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)

    username = factory.Sequence(lambda n: f"user_{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")
    is_staff = False
    is_superuser = False


class ProviderFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Provider
        django_get_or_create = ("name",)

    name = "panda"
    display_name = "Panda"
    active = True


class TagFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Tag

    name = factory.Sequence(lambda n: f"tag_{n}")
    scope = "artist"
    source = "user"


class GalleryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Gallery

    title = factory.Sequence(lambda n: f"Gallery {n}")
    gid = factory.Sequence(lambda n: f"{100000 + n}")
    token = factory.Sequence(lambda n: f"tok_{n}")
    provider = "panda"
    public = True


class ArchiveFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Archive

    title = factory.Sequence(lambda n: f"Archive {n}")
    user = factory.SubFactory(UserFactory)
    gallery = factory.SubFactory(GalleryFactory)


class WantedGalleryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WantedGallery

    title = factory.Sequence(lambda n: f"Wanted {n}")
    user = factory.SubFactory(UserFactory)
    provider = "panda"
    priority = 0
