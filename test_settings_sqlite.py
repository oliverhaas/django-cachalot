"""SQLite-only settings for the fast local test loop.

The default ``settings`` module always declares PostgreSQL and MySQL databases
plus Redis and Memcached caches, so running even one test requires all four
servers. This trims them to SQLite and locmem.
"""
from settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': 'cachalot.sqlite3',
        'TEST': {'NAME': 'test_cachalot.sqlite3'},
    },
    # A second alias is required: several existing tests (CommandTestCase,
    # MultiDatabaseTestCase) look for a database other than 'default'.
    'secondary': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': 'cachalot2.sqlite3',
        'TEST': {'NAME': 'test_cachalot2.sqlite3'},
    },
}
DATABASE_ROUTERS = []
# Two cache aliases, so SettingsTestCase.test_cache is not skipped.
CACHES = {
    'default': CACHES['default'],  # noqa: F405
    'locmem2': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'locmem2',
        'OPTIONS': {'MAX_ENTRIES': 10e9},
    },
}
