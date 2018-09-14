from django.dispatch import Signal


wanted_gallery_found = Signal(providing_args=["gallery", "wanted_gallery_list"])
