import typing

from PIL import Image as PImage
import numpy as np

if typing.TYPE_CHECKING:
    from viewer.models import Archive, WantedImage

try:
    import cv2 as cv
    CAN_USE_IMAGE_MATCH = True
except ModuleNotFoundError:
    # Error handling
    CAN_USE_IMAGE_MATCH = False


FLANN_INDEX_KDTREE = 1
FLANN_N_TREES = 5
FLANN_N_CHECKS = 50


def get_image_thumbnail_and_grayscale(archive: 'Archive') -> tuple[np.ndarray, np.ndarray]:
    img_thumbnail = cv.imread(archive.thumbnail.path)
    img_gray = cv.cvtColor(img_thumbnail, cv.COLOR_BGR2GRAY)
    return img_thumbnail, img_gray


def compare_wanted_with_image(img_gray: np.ndarray, img_thumbnail: np.ndarray, wanted_image: 'WantedImage', skip_minimum: bool = False) -> tuple[bool, int, typing.Optional[PImage.Image]]:
    template_img = cv.imread(wanted_image.thumbnail.path)
    template_gray = cv.cvtColor(template_img, cv.COLOR_BGR2GRAY)
    sift = cv.SIFT.create()
    kp1, des1 = sift.detectAndCompute(template_gray, None)  # type: ignore
    kp2, des2 = sift.detectAndCompute(img_gray, None)  # type: ignore
    # FLANN parameters
    index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=FLANN_N_TREES)
    search_params = dict(checks=FLANN_N_CHECKS)  # or pass empty dictionary
    flann = cv.FlannBasedMatcher(index_params, search_params)  # type: ignore
    matches = flann.knnMatch(des1, des2, k=2)
    good_matches = []
    found_match = False
    im = None
    for m, n in matches:
        if m.distance < wanted_image.match_threshold * n.distance:
            good_matches.append([m])
    if len(good_matches) > 0 and (len(good_matches) >= wanted_image.minimum_features or skip_minimum):

        if wanted_image.restrict_by_homogeneity:
            src_pts = np.array([kp1[m[0].queryIdx].pt for m in good_matches], dtype=np.float32)
            dst_pts = np.array([kp2[m[0].trainIdx].pt for m in good_matches], dtype=np.float32)

            slopes = (dst_pts[:, 1] - src_pts[:, 1]) / (dst_pts[:, 0] + template_img.shape[0] - src_pts[:, 0])
            slopes_mean = np.mean(slopes, axis=0)
            slopes_compared = np.abs((slopes / slopes_mean) - 1)
            distances = np.sqrt(
                (dst_pts[:, 1] - src_pts[:, 1]) ** 2 + (dst_pts[:, 0] + template_img.shape[0] - src_pts[:, 0]) ** 2)
            distances_mean = np.mean(distances, axis=0)
            distances_compared = np.abs((distances / distances_mean) - 1)

            if np.count_nonzero(slopes_compared > 0.1) == 0 and np.count_nonzero(distances_compared > 0.1) == 0:
                found_match = True
        else:
            found_match = True
        if found_match:
            draw_params = dict(matchColor=(0, 255, 0), singlePointColor=None, flags=2)
            img_matches = cv.drawMatchesKnn(template_img, kp1, img_thumbnail, kp2, good_matches, None, **draw_params)  # type: ignore
            im = PImage.fromarray(cv.cvtColor(img_matches, cv.COLOR_BGR2RGB))

    return found_match, len(good_matches), im
