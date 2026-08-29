import re
import types
from collections.abc import Iterable
from functools import wraps
from time import time

from django.core.exceptions import EmptyResultSet
from django.db.backends.utils import CursorWrapper
from django.db.models.signals import post_migrate
from django.db.models.sql.compiler import (
    SQLCompiler, SQLInsertCompiler, SQLUpdateCompiler, SQLDeleteCompiler,
)
from django.db.transaction import Atomic, get_connection

from .api import invalidate, LOCAL_STORAGE
from .cache import cachalot_caches
from .settings import cachalot_settings, ITERABLES
from .tenancy import (
    UNKNOWN, are_all_shared, get_tenant, observe_statement, pop_tenant,
    push_tenant, tenancy_enabled,
)
from .utils import (
    _get_table_cache_keys, _get_tables_from_sql, get_tenant_query_cache_key,
    UncachableQuery, is_cachable, filter_cachable,
)


WRITE_COMPILERS = (SQLInsertCompiler, SQLUpdateCompiler, SQLDeleteCompiler)

SQL_DATA_CHANGE_RE = re.compile(
    '|'.join([
        fr'(\W|\A){re.escape(keyword)}(\W|\Z)'
        for keyword in ['update', 'insert', 'delete', 'alter', 'create', 'drop']
    ]),
    flags=re.IGNORECASE,
)

def _unset_raw_connection(original):
    def inner(compiler, *args, **kwargs):
        compiler.connection.raw = False
        try:
            return original(compiler, *args, **kwargs)
        finally:
            compiler.connection.raw = True
    return inner


def _get_result_or_execute_query(execute_query_func, cache,
                                 cache_key, table_cache_keys):
    try:
        data = cache.get_many(table_cache_keys + [cache_key])
    except (KeyError, ModuleNotFoundError):
        data = None

    new_table_cache_keys = set(table_cache_keys)
    if data:
        new_table_cache_keys.difference_update(data)

        if not new_table_cache_keys:
            try:
                timestamp, result = data.pop(cache_key)
                if timestamp >= max(data.values()):
                    return result
            except (KeyError, TypeError, ValueError):
                # In case `cache_key` is not in `data` or contains bad data,
                # we simply run the query and cache again the results.
                pass

    result = execute_query_func()

    if result.__class__ == types.GeneratorType and not cachalot_settings.CACHALOT_CACHE_ITERATORS:
        return result

    if result.__class__ not in ITERABLES and isinstance(result, Iterable):
        result = list(result)

    now = time()
    to_be_set = {k: now for k in new_table_cache_keys}
    to_be_set[cache_key] = (now, result)
    cache.set_many(to_be_set, cachalot_settings.CACHALOT_TIMEOUT)

    return result


def _patch_compiler(original):
    @wraps(original)
    @_unset_raw_connection
    def inner(compiler, *args, **kwargs):
        execute_query_func = lambda: original(compiler, *args, **kwargs)
        # Checks if utils/cachalot_disabled
        if not getattr(LOCAL_STORAGE, "cachalot_enabled", True):
            return execute_query_func()

        db_alias = compiler.using
        if db_alias not in cachalot_settings.CACHALOT_DATABASES \
                or isinstance(compiler, WRITE_COMPILERS):
            return execute_query_func()

        tenant = get_tenant(compiler.connection)
        if tenant is UNKNOWN:
            # We cannot tell which tenant this query belongs to, so we must
            # not serve it from cache nor put it in one.
            return execute_query_func()

        try:
            cache_key = cachalot_settings.CACHALOT_QUERY_KEYGEN(compiler)
            tables, table_cache_keys = _get_table_cache_keys(compiler, tenant)
        except (EmptyResultSet, UncachableQuery):
            return execute_query_func()

        if tenant is not None and not are_all_shared(tables):
            cache_key = get_tenant_query_cache_key(cache_key, tenant)

        return _get_result_or_execute_query(
            execute_query_func,
            cachalot_caches.get_cache(db_alias=db_alias),
            cache_key, table_cache_keys)

    return inner


def _patch_write_compiler(original):
    @wraps(original)
    @_unset_raw_connection
    def inner(write_compiler, *args, **kwargs):
        db_alias = write_compiler.using
        table = write_compiler.query.get_meta().db_table
        if is_cachable(table):
            tenant = get_tenant(write_compiler.connection)
            invalidate(table, db_alias=db_alias,
                       cache_alias=cachalot_settings.CACHALOT_CACHE,
                       tenant=None if tenant is UNKNOWN else tenant)
        return original(write_compiler, *args, **kwargs)

    return inner


def _patch_orm():
    if cachalot_settings.CACHALOT_ENABLED:
        SQLCompiler.execute_sql = _patch_compiler(SQLCompiler.execute_sql)
    for compiler in WRITE_COMPILERS:
        compiler.execute_sql = _patch_write_compiler(compiler.execute_sql)


def _unpatch_orm():
    if hasattr(SQLCompiler.execute_sql, '__wrapped__'):
        SQLCompiler.execute_sql = SQLCompiler.execute_sql.__wrapped__
    for compiler in WRITE_COMPILERS:
        compiler.execute_sql = compiler.execute_sql.__wrapped__


def _patch_cursor():
    def _patch_cursor_execute(original, is_many=False):
        @wraps(original)
        def inner(cursor, sql, *args, **kwargs):
            params = None
            if not is_many:
                params = args[0] if args else kwargs.get('params')
            failed = False
            try:
                return original(cursor, sql, *args, **kwargs)
            except BaseException:
                # BaseException, not Exception: an interrupted statement did
                # not take effect either, and must not be trusted.
                failed = True
                raise
            finally:
                connection = cursor.db
                if isinstance(sql, bytes):
                    sql = sql.decode('utf-8')
                # `executemany` is never used to set a session variable, and
                # its parameter list has no positional mapping we could use.
                # `sql` is not always a str: psycopg3 accepts Composable
                # objects, which have no `.lower()`. Skipping them keeps an
                # AttributeError in this `finally` from masking the real
                # database error.
                if tenancy_enabled() and not is_many and isinstance(sql, str):
                    observe_statement(connection, sql, params, failed=failed)
                if (cachalot_settings.CACHALOT_INVALIDATE_RAW
                        and getattr(connection, 'raw', True)):
                    lowered = sql.lower()
                    if SQL_DATA_CHANGE_RE.search(lowered):
                        tables = filter_cachable(
                            _get_tables_from_sql(connection, lowered))
                        if tables:
                            tenant = get_tenant(connection)
                            invalidate(
                                *tables, db_alias=connection.alias,
                                cache_alias=cachalot_settings.CACHALOT_CACHE,
                                tenant=None if tenant is UNKNOWN else tenant)

        return inner

    if cachalot_settings.CACHALOT_INVALIDATE_RAW or tenancy_enabled():
        CursorWrapper.execute = _patch_cursor_execute(CursorWrapper.execute)
        CursorWrapper.executemany = _patch_cursor_execute(
            CursorWrapper.executemany, is_many=True)


def _unpatch_cursor():
    if hasattr(CursorWrapper.execute, '__wrapped__'):
        CursorWrapper.execute = CursorWrapper.execute.__wrapped__
        CursorWrapper.executemany = CursorWrapper.executemany.__wrapped__


def _patch_atomic():
    def patch_enter(original):
        @wraps(original)
        def inner(self):
            cachalot_caches.enter_atomic(self.using)
            original(self)
            # After `original`: if entering the block raises, `__exit__`
            # never runs, and a push made beforehand would never be popped.
            push_tenant(get_connection(self.using))

        return inner

    def patch_exit(original):
        @wraps(original)
        def inner(self, exc_type, exc_value, traceback):
            connection = get_connection(self.using)
            needs_rollback = connection.needs_rollback
            try:
                original(self, exc_type, exc_value, traceback)
            finally:
                committed = exc_type is None and not needs_rollback
                cachalot_caches.exit_atomic(self.using, committed)
                pop_tenant(connection, committed)

        return inner

    Atomic.__enter__ = patch_enter(Atomic.__enter__)
    Atomic.__exit__ = patch_exit(Atomic.__exit__)


def _unpatch_atomic():
    Atomic.__enter__ = Atomic.__enter__.__wrapped__
    Atomic.__exit__ = Atomic.__exit__.__wrapped__


def _invalidate_on_migration(sender, **kwargs):
    invalidate(*sender.get_models(), db_alias=kwargs['using'],
               cache_alias=cachalot_settings.CACHALOT_CACHE)


def patch():
    post_migrate.connect(_invalidate_on_migration)

    _patch_cursor()
    _patch_atomic()
    _patch_orm()


def unpatch():
    post_migrate.disconnect(_invalidate_on_migration)

    _unpatch_cursor()
    _unpatch_atomic()
    _unpatch_orm()
