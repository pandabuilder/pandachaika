from django.test import TestCase

from core.base.comparison import get_list_closer_gallery_titles_from_list
from viewer.models import Gallery


class CoreTest(TestCase):
    def setUp(self):
        # Galleries
        self.test_gallery1 = Gallery.objects.create(title='sample non public gallery 1', gid='344', provider='panda')
        self.test_gallery2 = Gallery.objects.create(title='sample non public gallery 2', gid='342', provider='test')

    def test_repeated_archives(self):

        title_to_check = 'my title'
        galleries_title_id = [(gallery.title, gallery.pk) for gallery in Gallery.objects.all()]
        cutoff = 0.4
        max_matches = 10

        similar_list = get_list_closer_gallery_titles_from_list(
            title_to_check, galleries_title_id, cutoff, max_matches)

        self.assertIsNone(similar_list)

        title_to_check_2 = 'public gallery 1'

        similar_list = get_list_closer_gallery_titles_from_list(
            title_to_check_2, galleries_title_id, cutoff, max_matches)

        self.assertIsNotNone(similar_list)
        self.assertEqual(len(similar_list), 2)
        self.assertEqual(similar_list[0][0], 'sample non public gallery 1')
        self.assertEqual(similar_list[0][2], 0.7441860465116279)
