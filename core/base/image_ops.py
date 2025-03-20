from PIL import Image
import numpy as np


def convert(img, target_type_min, target_type_max, target_type):
    i_min = img.min()
    i_max = img.max()

    a = (target_type_max - target_type_min) / (i_max - i_min)
    b = target_type_max - a * i_max
    new_img = (a * img + b).astype(target_type)
    return new_img


# Adapted from https://github.com/bertsky/core/blob/061bae37f79aaac5aea64cf9afb8e1429b8243f5/ocrd/ocrd/workspace.py#L407
def img_to_thumbnail(im: Image.Image, width: int = 200, height: int = 290) -> Image.Image:
    if im.mode.startswith("I") or im.mode.startswith("F"):
        arr_image = np.array(im)
        if arr_image.dtype.kind == "i" or arr_image.dtype.kind == "u":
            arr_image = convert(arr_image, 0, 255, np.uint8)
        elif arr_image.dtype.kind == "f":
            # float needs to be scaled from [0,1.0] to [0,255]
            arr_image *= 255
            arr_image = arr_image.astype(np.uint8)
        im = Image.fromarray(arr_image)
    else:
        if im.mode != "RGB":
            im = im.convert("RGB")
    im.thumbnail((width, height), Image.Resampling.LANCZOS)

    return im
