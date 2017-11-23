class Settings:
    def __init__(self):
        self.api_key = ''


def parse_config(global_settings, config):

    settings = Settings()
    if 'general' in config:
        if 'api_key' in config['general']:
            settings.api_key = config['general']['api_key']
    return settings
