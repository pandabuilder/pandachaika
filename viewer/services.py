from PIL import Image as PImage
from PIL import UnidentifiedImageError
from django.db.models import QuerySet

from core.base import hashing
from viewer.models import Archive


class CompareObjectsService:

    image_hash_functions = hashing.available_image_hash_functions
    file_hash_functions = hashing.available_file_hash_functions

    # code from imagehash library
    @staticmethod
    def alpharemover(image):
        if image.mode != 'RGBA':
            return image
        canvas = PImage.new('RGBA', image.size, (255, 255, 255, 255))
        canvas.paste(image, mask=image)
        return canvas.convert('RGB')

    @staticmethod
    def image_loader(hashfunc, hash_size=8):
        def inner_function(path: str):
            try:
                image = CompareObjectsService.alpharemover(PImage.open(path))
                return str(hashfunc(image))
            except UnidentifiedImageError:
                return 'null'
        return inner_function

    @classmethod
    def hash_archives(cls, archives: QuerySet[Archive], algorithms: list[str], thumbnails: bool = True, images: bool = True) -> dict:

        results_per_algorithm: dict[str, dict] = {

        }

        available_algos = [(x, cls.image_loader(cls.image_hash_functions[x])) for x in algorithms if x in cls.image_hash_functions]
        available_algos += [(x, cls.file_hash_functions[x]) for x in algorithms if x in cls.file_hash_functions]

        for algo_name, algo_func in available_algos:

            results: dict[str, dict] = {
                'archives': {
                },
                'images': {
                }
            }

            for archive in archives:
                if thumbnails:
                    thumbnail = archive.thumbnail
                    if thumbnail:
                        with open(thumbnail.path, "rb") as thumb:
                            results['archives'][archive.pk] = algo_func(thumb)
                if images:
                    if algo_name == 'sha1' and archive.image_set.filter(sha1__isnull=True).count() == 0:
                        image_result: dict[int, str] = {int(x.position): str(x.sha1) for x in archive.image_set.all()}
                        results['images'][archive.pk] = image_result
                    else:
                        archive_img_results = archive.hash_images_with_function(algo_func)
                        results['images'][archive.pk] = archive_img_results

            results_per_algorithm[algo_name] = results

        return results_per_algorithm
