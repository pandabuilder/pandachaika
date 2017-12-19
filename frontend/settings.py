"""
Django settings for frontend project.
"""

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
import os
from typing import Any, Dict

from core.base.setup import Settings
from core.base.utilities import module_exists

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

if 'PANDA_CONFIG_DIR' in os.environ:
    crawler_settings = Settings(load_from_disk=True, default_dir=os.environ['PANDA_CONFIG_DIR'])
else:
    crawler_settings = Settings(load_from_disk=True)

MAIN_LOGGER = crawler_settings.log_location

if not os.path.exists(os.path.dirname(MAIN_LOGGER)):
    os.makedirs(os.path.dirname(MAIN_LOGGER))

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = crawler_settings.django_secret_key

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = crawler_settings.django_debug_mode

# Might want to limit it here.
ALLOWED_HOSTS = ['*']

if crawler_settings.urls.behind_proxy:
    USE_X_FORWARDED_HOST = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

LOGGING: Dict[str, Any] = {
    'version': 1,
    'disable_existing_loggers': True,
    'formatters': {
        'verbose': {
            'format': '%(levelname)s %(asctime)-.19s %(module)s '
                      '%(process)d %(thread)d %(message)s[0m'
        },
        'simple': {
            'format': '%(levelname)s %(message)s[0m'
        },
        'standard': {
            '()': 'core.base.utilities.StandardFormatter',
            'format': '%(asctime)-.19s %(levelname)s [%(module)s] %(message)s'
        },

    },
    'handlers': {
        'viewer': {
            'level': 'DEBUG',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': MAIN_LOGGER,
            'maxBytes': 1024 * 1024 * 2,  # 2 MB
            'backupCount': 5,
            'encoding': 'utf8',
            'formatter': 'standard',
        },
    },
    'loggers': {
        'viewer.webcrawler': {
            'handlers': ['viewer'],
            'level': 'INFO'
        },
        'viewer.foldercrawler': {
            'handlers': ['viewer'],
            'level': 'INFO'
        },
        'viewer.frontend': {
            'handlers': ['viewer'],
            'level': 'INFO'
        },
        'viewer.threads': {
            'handlers': ['viewer'],
            'level': 'ERROR'
        },
        'django': {
            'handlers': ['viewer'],
            'propagate': True,
            'level': 'ERROR',
        },
    }
}


# Application definition

INSTALLED_APPS = [
    'dal',
    'dal_select2',
    'dal_jal',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'viewer',
]

if module_exists('compressor'):
    INSTALLED_APPS += ['compressor']

if module_exists('django_unused_media'):
    INSTALLED_APPS += ['django_unused_media']

if DEBUG and module_exists('debug_toolbar'):
    INSTALLED_APPS += ['debug_toolbar']

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

STATICFILES_FINDERS = [
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder'
]

STATICFILES_DIRS = (
    os.path.join(BASE_DIR, 'viewer/assets'),
)

if module_exists('compressor'):
    STATICFILES_FINDERS += ['compressor.finders.CompressorFinder']
    COMPRESS_CSS_FILTERS = [
        'compressor.filters.css_default.CssAbsoluteFilter',
        'compressor.filters.cssmin.rCSSMinFilter'
    ]
    COMPRESS_ENABLED = not DEBUG
    COMPRESS_OFFLINE = True

ROOT_URLCONF = 'frontend.urls'

WSGI_APPLICATION = 'frontend.wsgi.application'

# Debug Toolbar
if DEBUG and module_exists('debug_toolbar'):
    INTERNAL_IPS = ['127.0.0.1']
    DEBUG_TOOLBAR_PATCH_SETTINGS = False
    MIDDLEWARE = ['debug_toolbar.middleware.DebugToolbarMiddleware'] + \
        MIDDLEWARE


# Database
if crawler_settings.db_engine == 'mysql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': crawler_settings.database['mysql_name'],
            'USER': crawler_settings.database['mysql_user'],
            'PASSWORD': crawler_settings.database['mysql_password'],
            'HOST': crawler_settings.database['mysql_host'],
            'PORT': crawler_settings.database['mysql_port'],
            'OPTIONS': {
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
                'charset': 'utf8mb4'
            },
        }
    }
elif crawler_settings.db_engine == 'postgresql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql_psycopg2',
            'NAME': crawler_settings.database['postgresql_name'],
            'USER': crawler_settings.database['postgresql_user'],
            'PASSWORD': crawler_settings.database['postgresql_password'],
            'HOST': crawler_settings.database['postgresql_host'],
            'PORT': crawler_settings.database['postgresql_port'],
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': crawler_settings.database['sqlite_location'],
        }
    }

# Internationalization

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)

MAIN_URL = crawler_settings.urls.viewer_main_url

STATIC_ROOT = os.path.join(BASE_DIR, 'static/')
STATIC_URL = MAIN_URL.replace("/", "") + crawler_settings.urls.static_url

MEDIA_ROOT = crawler_settings.MEDIA_ROOT
MEDIA_URL = MAIN_URL.replace("/", "") + crawler_settings.urls.media_url

if MAIN_URL != '':
    STATIC_URL = "/" + STATIC_URL
    MEDIA_URL = "/" + MEDIA_URL
    CSRF_COOKIE_PATH = "/" + MAIN_URL
    SESSION_COOKIE_PATH = "/" + MAIN_URL

LOGIN_URL = 'viewer:login'
LOGOUT_URL = 'viewer:logout'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'debug': crawler_settings.django_debug_mode,
            'context_processors': [
                'django.contrib.messages.context_processors.messages',
                'django.contrib.auth.context_processors.auth',
                'django.template.context_processors.media',
                'django.template.context_processors.request',
            ]
        },
    },
]

# Mail settings
if crawler_settings.mail_logging.enable:
    EMAIL_HOST = crawler_settings.mail_logging.mailhost
    EMAIL_PORT = 587
    EMAIL_HOST_USER = crawler_settings.mail_logging.username
    EMAIL_HOST_PASSWORD = crawler_settings.mail_logging.password
    EMAIL_SUBJECT_PREFIX = crawler_settings.mail_logging.subject + " "
    SERVER_EMAIL = crawler_settings.mail_logging.from_
    ADMINS = [('Admin', crawler_settings.mail_logging.to), ]
    EMAIL_USE_TLS = True
    # EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

    LOGGING['handlers']['mail_admins'] = {
        'level': 'ERROR',
        'class': 'django.utils.log.AdminEmailHandler',
    }
    LOGGING['loggers']['django']['handlers'].append('mail_admins')

if crawler_settings.elasticsearch.enable:
    from elasticsearch import Elasticsearch, RequestsHttpConnection
    ES_CLIENT = Elasticsearch(
        [crawler_settings.elasticsearch.url],
        connection_class=RequestsHttpConnection
    )
    MAX_RESULT_WINDOW = crawler_settings.elasticsearch.max_result_window
    ES_AUTOREFRESH = crawler_settings.elasticsearch.auto_refresh
    ES_INDEX_NAME = crawler_settings.elasticsearch.index_name
else:
    MAX_RESULT_WINDOW = 10000
    ES_AUTOREFRESH = False
    ES_CLIENT = None
    ES_INDEX_NAME = 'viewer'

# These are the default providers, you could register more after the program starts, but that's not supported
# If for each new provider, you need to call this method to register it.
# For now, the problem is that if you disable a provider entirely, you can't reconstruct the original URL, since it's
# not stored on the database.
PROVIDERS = [
    'core.providers.generic',
    'core.providers.cafe',
    'core.providers.fakku',
    'core.providers.mugimugi',
    'core.providers.nhentai',
    'core.providers.panda',
    'core.providers.twitter',
]

PROVIDER_CONTEXT = crawler_settings.provider_context
CRAWLER_SETTINGS = crawler_settings
WORKERS = crawler_settings.workers
