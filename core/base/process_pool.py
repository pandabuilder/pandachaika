import atexit
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
import io
import logging
import multiprocessing
import os
import threading
import typing
import zipfile
import zlib

logger = logging.getLogger(__name__)

_process_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _pool_initializer() -> None:
    """Initializer for child processes in the ProcessPoolExecutor.

    Ensures child processes do not share open database connections or handles
    inherited from the parent process.
    """
    try:
        from django import db
        db.connections.close_all()
    except Exception:
        pass


def get_default_max_workers() -> int:
    """Determine the default number of worker processes for CPU-bound tasks.

    Uses settings.CRAWLER_SETTINGS.process_pool_workers if defined and positive;
    otherwise defaults to half of available CPU cores (at least 1).
    """
    try:
        from django.conf import settings
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        if crawler_settings and getattr(crawler_settings, "process_pool_workers", 0) > 0:
            return int(crawler_settings.process_pool_workers)
    except Exception:
        pass

    cpu = os.cpu_count() or 2
    return max(1, cpu // 2)


def get_process_pool(max_workers: int | None = None) -> ProcessPoolExecutor:
    """Get or create the singleton ProcessPoolExecutor."""
    global _process_pool
    with _pool_lock:
        if _process_pool is not None:
            if getattr(_process_pool, "_broken", False) or getattr(_process_pool, "_shutdown", False):
                try:
                    _process_pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                _process_pool = None

        if _process_pool is None:
            workers = max_workers or get_default_max_workers()
            ctx = multiprocessing.get_context()
            _process_pool = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=ctx,
                initializer=_pool_initializer,
            )
            logger.info("Initialized ProcessPoolExecutor with %d workers (context: %s)", workers, ctx)
        return _process_pool


def shutdown_process_pool(wait: bool = True, cancel_futures: bool = True) -> None:
    """Shutdown the singleton ProcessPoolExecutor if active."""
    global _process_pool
    with _pool_lock:
        if _process_pool is not None:
            try:
                _process_pool.shutdown(wait=wait, cancel_futures=cancel_futures)
                logger.info("ProcessPoolExecutor shut down successfully.")
            except Exception as e:
                logger.warning("Error shutting down ProcessPoolExecutor: %s", e)
            finally:
                _process_pool = None


# Automatically clean up worker processes on interpreter exit
atexit.register(shutdown_process_pool)


def run_in_pool[T](
    func: Callable[..., T],
    *args: typing.Any,
    timeout: float | None = None,
    fallback_in_process: bool = True,
    **kwargs: typing.Any,
) -> T:
    """Submit a task to the ProcessPoolExecutor and wait for its result.

    Releases the Python GIL during subprocess execution. If the process pool
    fails and fallback_in_process is True, executes the function synchronously.
    """
    try:
        pool = get_process_pool()
        future = pool.submit(func, *args, **kwargs)
        return future.result(timeout=timeout)
    except Exception as e:
        if fallback_in_process:
            logger.warning(
                "Process pool execution failed for %s, falling back to in-process: %s",
                getattr(func, "__name__", str(func)),
                e,
            )
            return func(*args, **kwargs)
        raise


def map_in_pool[T, R](
    func: Callable[[T], R],
    iterable: Iterable[T],
    timeout: float | None = None,
    chunksize: int = 1,
    fallback_in_process: bool = True,
) -> list[R]:
    """Map a function over an iterable using the ProcessPoolExecutor.

    Releases the Python GIL during subprocess execution. If the pool fails and
    fallback_in_process is True, maps over the iterable in-process.
    """
    items = list(iterable)
    if not items:
        return []
    try:
        pool = get_process_pool()
        return list(pool.map(func, items, timeout=timeout, chunksize=chunksize))
    except Exception as e:
        if fallback_in_process:
            logger.warning(
                "Process pool map failed for %s, falling back to in-process: %s",
                getattr(func, "__name__", str(func)),
                e,
            )
            return [func(item) for item in items]
        raise


# --- Standalone Worker Functions (Picklable across process boundaries) ---


def worker_calc_crc32(filepath: str, chunk_size: int = 1048576) -> str:
    """Calculate the CRC32 checksum of a file in 1MB chunks without newline scanning overhead."""
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")
    prev = 0
    with open(filepath, "rb") as f:
        while chunk := f.read(chunk_size):
            prev = zlib.crc32(chunk, prev)
    return "%X" % (prev & 0xFFFFFFFF)


def worker_calc_zip_fileinfo_and_crc(
    filepath: str, get_extra_data: bool = True
) -> tuple[int, int, str, typing.Any]:
    """Calculate filesize, filecount, CRC32, and extra member data for a zip file in a worker process."""
    from core.base.utilities import get_zip_fileinfo

    filesize, filecount, other_file_data = get_zip_fileinfo(filepath, get_extra_data=get_extra_data)
    crc32 = worker_calc_crc32(filepath)
    return filesize, filecount, crc32, other_file_data


def worker_calc_phash_for_zip_member(
    arguments: tuple[str, str, str | None],
) -> tuple[str, str | None]:
    """Calculate perceptual hash (pHash) for a single member inside a zip (or nested zip) file."""
    zip_path, member_name, nested_zip_name = arguments
    try:
        from PIL import Image as PImage
        import imagehash

        raw_data: bytes | None = None
        with zipfile.ZipFile(zip_path, "r") as my_zip:
            if nested_zip_name is None:
                with my_zip.open(member_name) as image_file:
                    raw_data = image_file.read()
            else:
                with my_zip.open(nested_zip_name) as nested_zip_file:
                    nested_zip_data = io.BytesIO(nested_zip_file.read())
                    with zipfile.ZipFile(nested_zip_data) as my_nested_zip:
                        with my_nested_zip.open(member_name) as image_file:
                            raw_data = image_file.read()

        if raw_data is None:
            return member_name, None

        im: PImage.Image = PImage.open(io.BytesIO(raw_data))
        if im.mode == "RGBA":
            canvas = PImage.new("RGBA", im.size, (255, 255, 255, 255))
            canvas.paste(im, mask=im)
            im = canvas.convert("RGB")
        elif im.mode != "RGB":
            im = im.convert("RGB")

        hash_result = str(imagehash.phash(im))
        im.close()
        return member_name, hash_result
    except Exception as e:
        logger.debug("Failed to calculate phash for %s in %s: %s", member_name, zip_path, e)
        return member_name, None


def worker_calc_image_hash(
    image_path_or_bytes: str | bytes,
    algorithm: str = "phash",
) -> str | None:
    """Calculate perceptual or cryptographic hash for an image path or raw bytes."""
    try:
        from PIL import Image as PImage
        from core.base import hashing

        fp: str | io.BytesIO = (
            io.BytesIO(image_path_or_bytes) if isinstance(image_path_or_bytes, bytes) else image_path_or_bytes
        )

        if algorithm in hashing.available_image_hash_functions:
            hash_func = hashing.available_image_hash_functions[algorithm]
            im: PImage.Image = PImage.open(fp)
            if im.mode == "RGBA":
                canvas = PImage.new("RGBA", im.size, (255, 255, 255, 255))
                canvas.paste(im, mask=im)
                im = canvas.convert("RGB")
            elif im.mode != "RGB":
                im = im.convert("RGB")
            res = str(hash_func(im))
            im.close()
            return res
        elif algorithm in hashing.available_file_hash_functions:
            file_hash_func = hashing.available_file_hash_functions[algorithm]
            if isinstance(fp, str):
                with open(fp, "rb") as f:
                    return str(file_hash_func(f))
            else:
                return str(file_hash_func(fp))
        return None
    except Exception as e:
        logger.debug("Failed to calculate %s for image: %s", algorithm, e)
        return None


def worker_generate_thumbnail_from_zip(
    zip_path: str,
    member_name: str,
    nested_zip_name: str | None,
    output_path: str,
    width: float = 250,
    height: float = 362.5,
) -> tuple[int, int] | None:
    """Extract member from zip, downsample to thumbnail dimensions using Lanczos, and write JPEG to output_path."""
    try:
        from PIL import Image as PImage
        from core.base.image_ops import img_to_thumbnail

        raw_data: bytes | None = None
        with zipfile.ZipFile(zip_path, "r") as my_zip:
            if nested_zip_name is None:
                with my_zip.open(member_name) as current_img:
                    raw_data = current_img.read()
            else:
                with my_zip.open(nested_zip_name) as current_zip:
                    with zipfile.ZipFile(current_zip) as my_nested_zip:
                        with my_nested_zip.open(member_name) as current_img:
                            raw_data = current_img.read()

        if raw_data is None:
            return None

        im: PImage.Image = PImage.open(io.BytesIO(raw_data))
        im = img_to_thumbnail(im, width, height)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        im.save(output_path, "JPEG")
        im_size = (im.size[0], im.size[1])
        im.close()
        return im_size
    except Exception as e:
        logger.error("Failed to generate thumbnail for %s (%s): %s", zip_path, member_name, e)
        return None


def worker_resize_image(
    input_path_or_bytes: str | bytes,
    output_path: str,
    width: float = 250,
    height: float = 362.5,
) -> tuple[int, int] | None:
    """Resize image to thumbnail dimensions using Lanczos and save JPEG to output_path."""
    try:
        from PIL import Image as PImage
        from core.base.image_ops import img_to_thumbnail

        fp: str | io.BytesIO = (
            io.BytesIO(input_path_or_bytes) if isinstance(input_path_or_bytes, bytes) else input_path_or_bytes
        )
        im: PImage.Image = PImage.open(fp)
        im = img_to_thumbnail(im, width, height)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        im.save(output_path, "JPEG")
        im_size = (im.size[0], im.size[1])
        im.close()
        return im_size
    except Exception as e:
        logger.error("Failed to resize image to %s: %s", output_path, e)
        return None


# --- High-Level Helper Functions (Bypass GIL from worker threads) ---


def calc_crc32_in_pool(filepath: str, timeout: float | None = None) -> str:
    """Calculate CRC32 in the ProcessPoolExecutor, releasing the calling thread's GIL."""
    return run_in_pool(worker_calc_crc32, filepath, timeout=timeout)


def recalc_archive_fileinfo_in_pool(
    filepath: str, get_extra_data: bool = True, timeout: float | None = None
) -> tuple[int, int, str, typing.Any]:
    """Calculate archive filesize, filecount, CRC32, and extra data in the ProcessPoolExecutor."""
    return run_in_pool(worker_calc_zip_fileinfo_and_crc, filepath, get_extra_data=get_extra_data, timeout=timeout)


def generate_thumbnail_in_pool(
    zip_path: str,
    member_name: str,
    nested_zip_name: str | None,
    output_path: str,
    width: float = 250,
    height: float = 362.5,
    timeout: float | None = None,
) -> tuple[int, int] | None:
    """Generate thumbnail for an archive member in the ProcessPoolExecutor, releasing the GIL."""
    return run_in_pool(
        worker_generate_thumbnail_from_zip,
        zip_path,
        member_name,
        nested_zip_name,
        output_path,
        width,
        height,
        timeout=timeout,
    )


def resize_image_in_pool(
    input_path_or_bytes: str | bytes,
    output_path: str,
    width: float = 250,
    height: float = 362.5,
    timeout: float | None = None,
) -> tuple[int, int] | None:
    """Resize an image to thumbnail dimensions in the ProcessPoolExecutor, releasing the GIL."""
    return run_in_pool(
        worker_resize_image,
        input_path_or_bytes,
        output_path,
        width,
        height,
        timeout=timeout,
    )


def calculate_phash_for_zip_members_in_pool(
    tasks: list[tuple[str, str, str | None]],
    timeout: float | None = None,
    chunksize: int | None = None,
) -> dict[str, str]:
    """Map pHash calculation across archive members in the ProcessPoolExecutor."""
    if not tasks:
        return {}
    if chunksize is None:
        chunksize = max(1, min(32, len(tasks) // (get_default_max_workers() * 4) or 1))
    results = map_in_pool(
        worker_calc_phash_for_zip_member,
        tasks,
        timeout=timeout,
        chunksize=chunksize,
    )
    return {filename: hash_val for filename, hash_val in results if hash_val}
