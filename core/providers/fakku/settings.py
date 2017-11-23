class Settings:
    def __init__(self):
        self.cookies = {}


def parse_config(global_settings, config):

    settings = Settings()

    if 'cookies' in config:
        settings.cookies.update(config['cookies'])
    return settings
