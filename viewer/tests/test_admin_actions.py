from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from viewer.admin import WantedGalleryAdmin
from viewer.models import WantedGallery, Category, Tag
from django.contrib.auth.models import User

class MockSuperUser:
    def __init__(self):
        self.username = "admin"
        self.is_active = True
        self.is_staff = True
        self.is_superuser = True
    
    def has_perm(self, perm):
        return True
    
    def has_module_perms(self, app_label):
        return True

class AdminActionsTestCase(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.admin = WantedGalleryAdmin(WantedGallery, self.site)
        self.factory = RequestFactory()
        self.gallery = WantedGallery.objects.create(title="Test Gallery")
        # Use filter() to get a QuerySet
        self.queryset = WantedGallery.objects.filter(id=self.gallery.id)
        self.user = User.objects.create_superuser('admin', 'admin@test.com', 'password')

    def test_add_wanted_categories(self):
        request = self.factory.post("/", {"extra_field": "Cat1, Cat2"})
        request.user = self.user
        request._messages = [] 
        # Mock message_user to avoid disjoint setup
        self.admin.message_user = lambda request, message: None
        
        self.admin.add_wanted_categories(request, self.queryset)
        
        self.assertEqual(self.gallery.categories.count(), 2)
        self.assertTrue(self.gallery.categories.filter(name="Cat1").exists())
        self.assertTrue(self.gallery.categories.filter(name="Cat2").exists())

    def test_set_wanted_categories(self):
        c1 = Category.objects.create(name="OldCat")
        self.gallery.categories.add(c1)
        
        request = self.factory.post("/", {"extra_field": "NewCat"})
        request.user = self.user
        self.admin.message_user = lambda request, message: None
        
        self.admin.set_wanted_categories(request, self.queryset)
        
        self.assertEqual(self.gallery.categories.count(), 1)
        self.assertTrue(self.gallery.categories.filter(name="NewCat").exists())
        self.assertFalse(self.gallery.categories.filter(name="OldCat").exists())

    def test_add_unwanted_tags(self):
        request = self.factory.post("/", {"extra_field": "scope:tag1, tag2"})
        request.user = self.user
        self.admin.message_user = lambda request, message: None
        
        self.admin.add_unwanted_tags(request, self.queryset)
        
        self.assertEqual(self.gallery.unwanted_tags.count(), 2)
        self.assertTrue(self.gallery.unwanted_tags.filter(scope="scope", name="tag1").exists())
        self.assertTrue(self.gallery.unwanted_tags.filter(scope="", name="tag2").exists())

    def test_set_unwanted_tags(self):
        t1 = Tag.objects.create(name="oldtag")
        self.gallery.unwanted_tags.add(t1)
        
        request = self.factory.post("/", {"extra_field": "newtag"})
        request.user = self.user
        self.admin.message_user = lambda request, message: None
        
        self.admin.set_unwanted_tags(request, self.queryset)
        
        self.assertEqual(self.gallery.unwanted_tags.count(), 1)
        self.assertTrue(self.gallery.unwanted_tags.filter(name="newtag").exists())
        self.assertFalse(self.gallery.unwanted_tags.filter(name="oldtag").exists())
