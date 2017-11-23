import os


class Settings:
    def __init__(self):
        self.archive_dl_folder = ''


def parse_config(global_settings, config):

    settings = Settings()

    if 'locations' in config:
        if 'archive_dl_folder' in config['locations']:
            settings.archive_dl_folder = config['locations']['archive_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.archive_dl_folder))
        else:
            settings.archive_dl_folder = global_settings.archive_dl_folder
    return settings
