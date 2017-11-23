import os


class Settings:
    def __init__(self):
        self.cookies = {}
        self.torrent_dl_folder = ''


def parse_config(global_settings, config):

    settings = Settings()

    if 'cookies' in config:
        settings.cookies.update(config['cookies'])
    if 'locations' in config:
        if 'torrent_dl_folder' in config['locations']:
            settings.torrent_dl_folder = config['locations']['torrent_dl_folder']
            if not os.path.exists(os.path.join(global_settings.MEDIA_ROOT, settings.torrent_dl_folder)):
                os.makedirs(os.path.join(global_settings.MEDIA_ROOT, settings.torrent_dl_folder))
        else:
            settings.torrent_dl_folder = global_settings.torrent_dl_folder
    return settings
