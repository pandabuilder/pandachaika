class Settings:
    def __init__(self):
        self.token = ''
        self.token_secret = ''
        self.consumer_key = ''
        self.consumer_secret = ''


def parse_config(global_settings, config):

    settings = Settings()
    if 'general' in config:
        if 'token' in config['general']:
            settings.token = config['general']['token']
        if 'token_secret' in config['general']:
            settings.token_secret = config['general']['token_secret']
        if 'consumer_key' in config['general']:
            settings.consumer_key = config['general']['consumer_key']
        if 'consumer_secret' in config['general']:
            settings.consumer_secret = config['general']['consumer_secret']
    return settings
