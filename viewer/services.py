import typing
from typing import Optional, Union

from PIL import Image as PImage
from PIL import UnidentifiedImageError
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet

from core.base import hashing

if typing.TYPE_CHECKING:
    from viewer.models import Archive, Image, ItemProperties


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
        def inner_function(path: Union[typing.IO, typing.BinaryIO, str]):
            try:
                image = CompareObjectsService.alpharemover(PImage.open(path))
                return str(hashfunc(image))
            except UnidentifiedImageError:
                return 'null'
        return inner_function

    @classmethod
    def hash_archives(
            cls, archives: 'QuerySet[Archive]', algorithms: list[str], thumbnails: bool = True, images: bool = True,
            item_model: typing.Optional['typing.Type[ItemProperties]'] = None,
            image_model: typing.Optional['typing.Type[Image]'] = None
    ) -> dict:

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

                        if item_model and image_model:

                            image_type = ContentType.objects.get_for_model(image_model)

                            images_phashes = item_model.objects.filter(
                                content_type=image_type, object_id__in=archive.image_set.all(), tag='hash-compare',
                                name=algo_name
                            )

                            if images_phashes:
                                archive_img_results = {item.content_object.archive_position: item.value for item in images_phashes if item.content_object}
                                results['images'][archive.pk] = archive_img_results
                            else:
                                archive_img_results = archive.hash_images_with_function(algo_func)
                                results['images'][archive.pk] = archive_img_results
                        else:
                            archive_img_results = archive.hash_images_with_function(algo_func)
                            results['images'][archive.pk] = archive_img_results

            results_per_algorithm[algo_name] = results

        return results_per_algorithm

    @classmethod
    def hash_thumbnail(cls, fp: Union[typing.IO, str], algorithm: str) -> Optional[str]:

        if isinstance(fp, str):
            file_object: Union[typing.IO, typing.BinaryIO] = open(fp, "rb")
        else:
            file_object = fp

        chosen_algo = None

        if algorithm in cls.file_hash_functions:
            chosen_algo = cls.file_hash_functions[algorithm]

        if algorithm in cls.image_hash_functions:
            chosen_algo = cls.image_loader(cls.image_hash_functions[algorithm])

        if not chosen_algo:
            return None

        result = chosen_algo(file_object)
        return result
