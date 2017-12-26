"""
This file demonstrates two different styles of tests (one doctest and one
unittest). These will both pass when you run "manage.py test".

Replace these with more appropriate tests for your application.
"""
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

from viewer.models import Tag, Archive


class TagTestCase(TestCase):
    def setUp(self):
        Tag.objects.create(name="sister", scope="female")
        Tag.objects.create(name="hisasi", scope="artist")

    def test_first_artist_tag(self):
        """Test obtain first artist tag"""
        artist_tag = Tag.objects.first_artist_tag()
        self.assertIsNotNone(artist_tag)

    def test_tag_formatting(self):
        """Test default tag formatting"""
        tag = Tag.objects.get(scope='female', name='sister')
        self.assertIn(':', str(tag))


class PrivateURLsTest(TestCase):
    def setUp(self):

        # Admin user
        test_admin1 = User.objects.create_user(username='admin1', password='12345')
        test_admin1.is_staff = True
        test_admin1.save()

        # Registered user
        test_user1 = User.objects.create_user(username='testuser1', password='12345')
        test_user1.save()

        # Archives
        self.test_book1 = Archive.objects.create(title='sample non public archive', user=test_admin1)
        self.test_book2 = Archive.objects.create(title='sample public archive', user=test_admin1, public=True)
        self.test_book2 = Archive.objects.create(title='new public archive sample', user=test_admin1, public=True)

    def test_redirect_if_not_logged_in(self):
        """Test to deny access to log page"""
        resp = self.client.get(reverse('viewer:logs'))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith('/login/?next=/logs/'))

    def test_HTTP404_for_invalid_archive(self):
        self.client.login(username='testuser1', password='12345')
        resp = self.client.get(reverse('viewer:archive', args=[9999]))
        self.assertEqual(resp.status_code, 404)
        self.client.logout()
        resp = self.client.get(reverse('viewer:archive', args=[9999]))
        self.assertEqual(resp.status_code, 404)

    def test_HTTP404_for_non_public_archive_if_not_logged_in(self):
        resp = self.client.get(reverse('viewer:archive', args=[self.test_book1.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_for_public_archive_if_not_logged_in(self):
        resp = self.client.get(reverse('viewer:archive', args=[self.test_book2.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_public_archive_search(self):
        response = self.client.get(reverse('viewer:archive-search'), {'title': 'archive'})
        self.assertEqual(response.status_code, 200)
        # Check that pagination works (only 1 page)
        self.assertFalse(response.context['results'].has_next())
        self.assertFalse(response.context['results'].has_previous())
        # Check that the rendered context contains 2 archives.
        self.assertEqual(len(response.context['results']), 2)

    def test_logged_in_archive_search(self):
        # Login, and get the 3 archives.
        c = Client()
        c.login(username='testuser1', password='12345')
        response = c.get(reverse('viewer:archive-search'), {'title': 'archive'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['results']), 3)

    def test_quick_search(self):
        # c.login(username='fred', password='secret')
        response = self.client.get(reverse('viewer:archive-search'), {'qsearch': 'archive'})
        self.assertEqual(response.status_code, 200)
        # Check that pagination works (only 1 page)
        self.assertFalse(response.context['results'].has_next())
        self.assertFalse(response.context['results'].has_previous())
        # Check that the rendered context contains 2 archives.
        self.assertEqual(len(response.context['results']), 2)
