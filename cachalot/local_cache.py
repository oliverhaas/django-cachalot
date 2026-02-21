import threading
from collections import defaultdict

from django.core.cache.backends.locmem import LocMemCache

from .settings import cachalot_settings
from .signals import post_invalidation


_MISS = object()

_l1_cache = None
_table_to_keys = defaultdict(set)
_lock = threading.Lock()


def _get_l1_cache():
    global _l1_cache
    if _l1_cache is None:
        _l1_cache = LocMemCache('cachalot-l1', {'MAX_ENTRIES': 10000})
    return _l1_cache


def get_local_ttl(tables):
    """Return min TTL if ALL tables are in CACHALOT_LOCAL_CACHE_TABLES, else None."""
    local_tables = cachalot_settings.CACHALOT_LOCAL_CACHE_TABLES
    if not local_tables:
        return None
    ttls = []
    for table in tables:
        ttl = local_tables.get(table)
        if ttl is None:
            return None
        ttls.append(ttl)
    return min(ttls) if ttls else None


def local_get(cache_key):
    """Get from L1 cache. Returns the result on hit, _MISS sentinel on miss."""
    return _get_l1_cache().get(cache_key, _MISS)


def local_set(cache_key, result, tables, ttl):
    """Store in L1 cache with TTL and track table->key mapping."""
    _get_l1_cache().set(cache_key, result, ttl)
    with _lock:
        for table in tables:
            _table_to_keys[table].add(cache_key)


def local_invalidate(table):
    """Remove all L1 entries involving the given table."""
    if not cachalot_settings.CACHALOT_LOCAL_CACHE_TABLES:
        return
    if table not in cachalot_settings.CACHALOT_LOCAL_CACHE_TABLES:
        return
    l1 = _get_l1_cache()
    with _lock:
        keys = _table_to_keys.pop(table, set())
    for key in keys:
        l1.delete(key)


def _on_post_invalidation(sender, **kwargs):
    """Signal handler: invalidate L1 when a table is invalidated."""
    local_invalidate(sender)


post_invalidation.connect(_on_post_invalidation)
