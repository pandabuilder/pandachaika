from typing import Callable

from core.base.utilities import sha1_from_file_object

available_image_hash_functions: dict[str, Callable] = {

}

available_file_hash_functions: dict[str, Callable] = {

}

try:
    import imagehash

    available_image_hash_functions = {**available_image_hash_functions, **dict([
        ('ahash', imagehash.average_hash),
        ('phash', imagehash.phash),
        ('dhash', imagehash.dhash),
        ('whash-haar', imagehash.whash),
        ('whash-db4', lambda img: imagehash.whash(img, mode='db4')),
        ('colorhash', imagehash.colorhash),
    ])}
except ImportError:
    pass

available_file_hash_functions['sha1'] = sha1_from_file_object
