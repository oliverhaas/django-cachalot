import re
import threading
from contextlib import contextmanager
from random import Random
from unittest import skipUnless

from django.contrib.auth.models import User
from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.test import TransactionTestCase, override_settings, SimpleTestCase

from ..api import get_last_invalidation, invalidate
from ..cache import cachalot_caches
from ..settings import cachalot_settings
from ..signals import post_invalidation
from ..tenancy import (
    NOT_A_SET, UNKNOWN, are_all_shared, get_tenant, is_partitioned,
    observe_statement, parse_tenant_statement, pop_tenant, push_tenant,
)
from ..utils import (
    TENANT_TABLE_SUFFIX, get_read_table_cache_keys, get_table_cache_key,
    get_tenant_query_cache_key, get_write_table_cache_keys,
)
from .models import Test, TestParent
from .test_utils import FilteredTransactionTestCase, TestUtilsMixin


class TenantStateMixin:
    """
    Clear the tenant bookkeeping a test leaves behind on the connection.

    ``_cachalot_session_tenant_dirty`` is sticky by design, so a test that
    makes a session-scoped write would otherwise stop every test after it on
    this connection from caching anything.
    """

    def tearDown(self):
        super().tearDown()
        connection._cachalot_tenant = None
        connection._cachalot_tenant_stack = []
        connection._cachalot_session_tenant_dirty = False


class TenancySettingsTestCase(TransactionTestCase):
    def test_defaults_are_inert(self):
        self.assertIsNone(cachalot_settings.CACHALOT_TENANT_SETTING)
        self.assertEqual(cachalot_settings.CACHALOT_PARTITIONED_TABLES,
                         frozenset())
        self.assertEqual(cachalot_settings.CACHALOT_TENANT_SHARED_TABLES,
                         frozenset())

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=('cachalot_test',))
    def test_partitioned_tables_converted_to_frozenset(self):
        self.assertEqual(cachalot_settings.CACHALOT_TENANT_SETTING,
                         'app.tenant_id')
        self.assertEqual(cachalot_settings.CACHALOT_PARTITIONED_TABLES,
                         frozenset(('cachalot_test',)))

    @override_settings(CACHALOT_PARTITIONED_APPS=('cachalot',))
    def test_partitioned_apps_expand_to_table_names(self):
        self.assertIn('cachalot_test',
                      cachalot_settings.CACHALOT_PARTITIONED_TABLES)

    @override_settings(CACHALOT_TENANT_SHARED_TABLES=('cachalot_testparent',))
    def test_shared_tables_converted_to_frozenset(self):
        self.assertEqual(cachalot_settings.CACHALOT_TENANT_SHARED_TABLES,
                         frozenset(('cachalot_testparent',)))


@override_settings(CACHALOT_TENANT_SETTING='app.tenant_id')
class ParseTenantStatementTestCase(SimpleTestCase):
    def parse(self, sql, params=None):
        return parse_tenant_statement(sql, params)

    def test_unrelated_statements(self):
        for sql in ('SELECT 1',
                    'UPDATE cachalot_test SET name = %s',
                    "SET LOCAL statement_timeout = '5s'",
                    "SELECT set_config('app.other', '1', true)"):
            self.assertIs(self.parse(sql, ['x']), NOT_A_SET, sql)

    def test_set_local_literal(self):
        self.assertEqual(self.parse("SET LOCAL app.tenant_id = '42'"), '42')
        self.assertEqual(self.parse("set local app.tenant_id to '42'"), '42')
        self.assertEqual(self.parse('SET LOCAL app.tenant_id = 42'), '42')
        self.assertEqual(
            self.parse('SET LOCAL "app.tenant_id" = \'42\''), '42')

    def test_set_local_placeholder(self):
        self.assertEqual(self.parse('SET LOCAL app.tenant_id = %s', ['42']),
                         '42')
        self.assertEqual(self.parse('SET LOCAL app.tenant_id = %s', [42]),
                         '42')

    def test_set_config(self):
        self.assertEqual(
            self.parse("SELECT set_config('app.tenant_id', '42', true)"), '42')
        self.assertEqual(
            self.parse("SELECT set_config('app.tenant_id', %s, true)", ['42']),
            '42')
        self.assertEqual(
            self.parse('SELECT set_config(%s, %s, true)',
                       ['app.tenant_id', '42']),
            '42')

    def test_set_config_on_another_guc(self):
        self.assertIs(self.parse('SELECT set_config(%s, %s, true)',
                                 ['app.other', '42']),
                      NOT_A_SET)

    def test_clearing_forms(self):
        for sql in ('RESET app.tenant_id',
                    'SET LOCAL app.tenant_id TO DEFAULT',
                    "SELECT set_config('app.tenant_id', NULL, true)"):
            self.assertIsNone(self.parse(sql), sql)

    def test_non_local_is_unknown(self):
        self.assertIs(self.parse("SET app.tenant_id = '42'"), UNKNOWN)
        self.assertIs(self.parse("SET SESSION app.tenant_id = '42'"), UNKNOWN)
        self.assertIs(
            self.parse("SELECT set_config('app.tenant_id', '42', false)"),
            UNKNOWN)

    def test_unresolvable_values_are_unknown(self):
        self.assertIs(
            self.parse('SET LOCAL app.tenant_id = %(t)s', {'t': '42'}),
            UNKNOWN)
        self.assertIs(self.parse('SET LOCAL app.tenant_id = current_user'),
                      UNKNOWN)
        self.assertIs(
            self.parse("SELECT set_config('app.tenant_id', '42', %s)", [True]),
            UNKNOWN)
        self.assertIs(self.parse('SET LOCAL app.tenant_id = %s', []), UNKNOWN)

    def test_disabled_feature_parses_nothing(self):
        with override_settings(CACHALOT_TENANT_SETTING=None):
            self.assertIs(self.parse("SET LOCAL app.tenant_id = '42'"),
                          NOT_A_SET)

    def test_multiple_constructs_fail_closed(self):
        self.assertIs(
            self.parse("SET LOCAL app.tenant_id = '1'; "
                       "SET LOCAL app.tenant_id = '2'"),
            UNKNOWN)
        self.assertIs(
            self.parse("SET LOCAL app.tenant_id = '42'; RESET app.tenant_id"),
            UNKNOWN)

    def test_quoted_and_commented_out_constructs_are_ignored(self):
        for sql in (
                "INSERT INTO log (msg) VALUES "
                "('SET LOCAL app.tenant_id = ''9''')",
                "SELECT 1 /* SET LOCAL app.tenant_id = '9' */",
                "SELECT * FROM t WHERE note = 'reset app.tenant_id'",
                "SELECT 1 -- SET LOCAL app.tenant_id = '9'",
                'SELECT $$ RESET app.tenant_id $$',
                "SELECT $tag$ SET app.tenant_id = '9' $tag$",
                r"SELECT E'\' SET LOCAL app.tenant_id = ''9'''",
                'SELECT * FROM "SET LOCAL app.tenant_id = 1"',
                # Block comments nest in PostgreSQL: the first `*/` closes
                # only the inner one.
                '/* outer /* RESET app.tenant_id */ still open */ SELECT 1',
                # An unterminated literal protects the rest of the statement.
                "SELECT * FROM t WHERE note = 'oops; "
                "SET LOCAL app.tenant_id = 9",
        ):
            self.assertIs(self.parse(sql), NOT_A_SET, sql)

    def test_a_real_set_local_survives_a_decoy(self):
        self.assertEqual(
            self.parse("SET LOCAL app.tenant_id = '9' -- app.tenant_id again"),
            '9')
        self.assertEqual(
            self.parse("/* app.tenant_id */ SET LOCAL app.tenant_id = '9'"),
            '9')
        self.assertEqual(
            self.parse("SET LOCAL app.tenant_id = '9'; INSERT INTO log (msg) "
                       "VALUES ('RESET app.tenant_id')"),
            '9')

    def test_placeholder_inside_a_literal_still_counts(self):
        self.assertEqual(
            self.parse("INSERT INTO log (msg) VALUES ('a %s b'); "
                       'SET LOCAL app.tenant_id = %s', ['msg', '9']),
            '9')

    def test_psycopg3_placeholders_shift_the_index_too(self):
        self.assertEqual(
            self.parse('INSERT INTO log (blob) VALUES (%b); '
                       'SET LOCAL app.tenant_id = %s', ['blob', '9']),
            '9')

    def test_escaped_percent_is_not_a_placeholder(self):
        self.assertEqual(
            self.parse("INSERT INTO log (msg) VALUES ('%%s'); "
                       'SET LOCAL app.tenant_id = %s', ['9', 'junk']),
            '9')

    def test_pyformat_constructs_are_unresolvable_not_absent(self):
        for sql, params in (
                ('SET LOCAL app.tenant_id = %(t)s', {'t': '42'}),
                ("SELECT set_config('app.tenant_id', %(t)s, true)",
                 {'t': '42'}),
                ('SELECT set_config(%(n)s, %(t)s, true)',
                 {'n': 'app.tenant_id', 't': '42'}),
        ):
            self.assertIs(self.parse(sql, params), UNKNOWN, sql)

    def test_pyformat_elsewhere_does_not_make_a_construct(self):
        self.assertIs(
            self.parse('SELECT 1 /* app.tenant_id */ WHERE x = %(v)s',
                       {'v': 1}),
            NOT_A_SET)

    def test_dollar_inside_an_identifier_opens_nothing(self):
        self.assertEqual(
            self.parse("SELECT a$b$c; SET LOCAL app.tenant_id = '9'"), '9')

    def test_unrelated_trailing_statement_ignored(self):
        self.assertEqual(self.parse("SET LOCAL app.tenant_id = '7'; SELECT 1"),
                         '7')


class TenancyEnabledTestCase(SimpleTestCase):
    def test_enabled_when_setting_is_none(self):
        from ..tenancy import tenancy_enabled
        with override_settings(CACHALOT_TENANT_SETTING=None):
            self.assertFalse(tenancy_enabled())

    def test_enabled_when_setting_is_configured(self):
        from ..tenancy import tenancy_enabled
        with override_settings(CACHALOT_TENANT_SETTING='app.tenant_id'):
            self.assertTrue(tenancy_enabled())


@override_settings(CACHALOT_TENANT_SETTING='app.tenant_id')
class ConnectionTenantTestCase(TenantStateMixin, TransactionTestCase):
    def test_no_tenant_outside_a_transaction(self):
        observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
        self.assertIsNone(get_tenant(connection))

    def test_tenant_recorded_inside_a_transaction(self):
        with transaction.atomic():
            self.assertIsNone(get_tenant(connection))
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            self.assertEqual(get_tenant(connection), '42')
        self.assertIsNone(get_tenant(connection))

    def test_unrelated_statement_leaves_tenant_alone(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            observe_statement(connection, 'SELECT 1')
            self.assertEqual(get_tenant(connection), '42')

    def test_failed_statement_is_unknown(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'",
                              failed=True)
            self.assertIs(get_tenant(connection), UNKNOWN)

    def test_unparsable_statement_is_unknown(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            observe_statement(connection, "SET app.tenant_id = '43'")
            self.assertIs(get_tenant(connection), UNKNOWN)

    def test_committed_nested_atomic_keeps_its_tenant(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            push_tenant(connection)
            observe_statement(connection, "SET LOCAL app.tenant_id = '43'")
            self.assertEqual(get_tenant(connection), '43')
            pop_tenant(connection)
            # PostgreSQL keeps a SET LOCAL made inside a released savepoint.
            self.assertEqual(get_tenant(connection), '43')

    def test_rolled_back_nested_atomic_restores_its_outer_tenant(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            push_tenant(connection)
            observe_statement(connection, "SET LOCAL app.tenant_id = '43'")
            pop_tenant(connection, committed=False)
            self.assertEqual(get_tenant(connection), '42')

    def test_pop_without_push_clears(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            pop_tenant(connection)
            self.assertIsNone(get_tenant(connection))

    def test_session_scoped_set_distrusts_the_connection(self):
        observe_statement(connection, "SET app.tenant_id = '42'")
        self.assertIs(get_tenant(connection), UNKNOWN)
        with transaction.atomic():
            self.assertIs(get_tenant(connection), UNKNOWN)

    def test_set_local_masks_a_distrusted_session_value(self):
        observe_statement(connection, "SET app.tenant_id = '42'")
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '43'")
            self.assertEqual(get_tenant(connection), '43')
        self.assertIs(get_tenant(connection), UNKNOWN)

    def test_reset_trusts_the_connection_again(self):
        observe_statement(connection, "SET app.tenant_id = '42'")
        observe_statement(connection, 'RESET app.tenant_id')
        self.assertIsNone(get_tenant(connection))

    def test_reset_inside_a_transaction_keeps_the_connection_distrusted(self):
        # A rollback would put the session value back without telling us.
        observe_statement(connection, "SET app.tenant_id = '42'")
        with transaction.atomic():
            observe_statement(connection, 'RESET app.tenant_id')
            self.assertIs(get_tenant(connection), UNKNOWN)
        self.assertIs(get_tenant(connection), UNKNOWN)

    def test_local_pyformat_set_does_not_distrust_the_connection(self):
        with transaction.atomic():
            observe_statement(connection, 'SET LOCAL app.tenant_id = %(t)s',
                              {'t': '42'})
            self.assertIs(get_tenant(connection), UNKNOWN)
        self.assertIsNone(get_tenant(connection))

    def test_failed_reset_leaves_the_connection_distrusted(self):
        observe_statement(connection, "SET app.tenant_id = '42'")
        observe_statement(connection, 'RESET app.tenant_id', failed=True)
        self.assertIs(get_tenant(connection), UNKNOWN)

    def test_disabled_feature_records_nothing(self):
        with override_settings(CACHALOT_TENANT_SETTING=None):
            with transaction.atomic():
                observe_statement(connection,
                                  "SET LOCAL app.tenant_id = '42'")
                self.assertIsNone(get_tenant(connection))

    def test_outermost_push_clears_dirty_connection(self):
        with transaction.atomic():
            connection._cachalot_tenant = '99'
            connection._cachalot_tenant_stack = []
            push_tenant(connection)
            self.assertIsNone(get_tenant(connection))

    def test_nested_push_does_not_clear(self):
        with transaction.atomic():
            push_tenant(connection)
            observe_statement(connection, "SET LOCAL app.tenant_id = '7'")
            self.assertEqual(get_tenant(connection), '7')
            push_tenant(connection)
            self.assertEqual(get_tenant(connection), '7')


class TablePredicatesTestCase(SimpleTestCase):
    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=('cachalot_test',))
    def test_is_partitioned(self):
        self.assertTrue(is_partitioned('cachalot_test'))
        self.assertFalse(is_partitioned('cachalot_testparent'))

    @override_settings(CACHALOT_PARTITIONED_TABLES=('cachalot_test',))
    def test_is_partitioned_requires_the_feature_to_be_on(self):
        self.assertFalse(is_partitioned('cachalot_test'))

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_TENANT_SHARED_TABLES=('cachalot_testparent',))
    def test_are_all_shared(self):
        self.assertTrue(are_all_shared({'cachalot_testparent'}))
        self.assertFalse(are_all_shared({'cachalot_testparent',
                                         'cachalot_test'}))
        self.assertFalse(are_all_shared(set()))

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=('cachalot_testparent',),
                       CACHALOT_TENANT_SHARED_TABLES=('cachalot_testparent',))
    def test_partitioned_beats_shared(self):
        self.assertFalse(are_all_shared({'cachalot_testparent'}))

    @override_settings(CACHALOT_TENANT_SHARED_TABLES=('cachalot_testparent',))
    def test_are_all_shared_when_feature_disabled(self):
        self.assertTrue(are_all_shared({'cachalot_test'}))


DB = DEFAULT_DB_ALIAS
PARTITIONED = 'cachalot_test'
PLAIN = 'auth_user'


class TableCacheKeysTestCase(SimpleTestCase):
    def legacy_key(self, table):
        return get_table_cache_key(DB, table)

    def test_disabled_feature_produces_legacy_keys(self):
        for tenant in (None, '42'):
            self.assertEqual(
                get_read_table_cache_keys(DB, PARTITIONED, tenant),
                [self.legacy_key(PARTITIONED)])
            self.assertEqual(
                get_write_table_cache_keys(DB, PARTITIONED, tenant),
                [self.legacy_key(PARTITIONED)])

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))
    def test_partitioned_table_keys(self):
        k_any = self.legacy_key(PARTITIONED)
        read_unscoped = get_read_table_cache_keys(DB, PARTITIONED, None)
        read_scoped = get_read_table_cache_keys(DB, PARTITIONED, '42')
        write_unscoped = get_write_table_cache_keys(DB, PARTITIONED, None)
        write_scoped = get_write_table_cache_keys(DB, PARTITIONED, '42')

        self.assertEqual(read_unscoped, [k_any])
        self.assertEqual(write_unscoped[0], k_any)
        self.assertEqual(write_scoped[0], k_any)

        k_glob = write_unscoped[1]
        k_ten = write_scoped[1]
        self.assertEqual(read_scoped, [k_glob, k_ten])
        self.assertEqual(len({k_any, k_glob, k_ten}), 3)

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))
    def test_tenants_get_distinct_keys(self):
        self.assertNotEqual(get_read_table_cache_keys(DB, PARTITIONED, '42'),
                            get_read_table_cache_keys(DB, PARTITIONED, '43'))

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))
    def test_non_partitioned_table_is_untouched(self):
        for tenant in (None, '42'):
            self.assertEqual(get_read_table_cache_keys(DB, PLAIN, tenant),
                             [self.legacy_key(PLAIN)])
            self.assertEqual(get_write_table_cache_keys(DB, PLAIN, tenant),
                             [self.legacy_key(PLAIN)])

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))
    def test_tenant_normalised_to_str(self):
        self.assertEqual(get_read_table_cache_keys(DB, PARTITIONED, 42),
                         get_read_table_cache_keys(DB, PARTITIONED, '42'))
        self.assertEqual(get_write_table_cache_keys(DB, PARTITIONED, 42),
                         get_write_table_cache_keys(DB, PARTITIONED, '42'))

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))
    def test_unknown_tenant_normalised_to_none_in_key_functions(self):
        # The same single legacy key as an unscoped tenant, not one derived
        # from the sentinel's repr.
        self.assertEqual(get_read_table_cache_keys(DB, PARTITIONED, UNKNOWN),
                         get_read_table_cache_keys(DB, PARTITIONED, None))
        self.assertEqual(get_write_table_cache_keys(DB, PARTITIONED, UNKNOWN),
                         get_write_table_cache_keys(DB, PARTITIONED, None))

    def test_tenant_query_cache_key(self):
        base = 'a' * 40
        self.assertNotEqual(get_tenant_query_cache_key(base, '42'), base)
        self.assertNotEqual(get_tenant_query_cache_key(base, '42'),
                            get_tenant_query_cache_key(base, '43'))
        self.assertEqual(get_tenant_query_cache_key(base, '42'),
                         get_tenant_query_cache_key(base, '42'))

    def test_tenant_query_cache_key_normalised_to_str(self):
        base = 'a' * 40
        self.assertEqual(get_tenant_query_cache_key(base, 42),
                         get_tenant_query_cache_key(base, '42'))


@override_settings(CACHALOT_TENANT_SETTING=None,
                   CACHALOT_PARTITIONED_TABLES=(PARTITIONED,),
                   CACHALOT_TENANT_SHARED_TABLES=(PARTITIONED,))
class DisabledFeatureTestCase(SimpleTestCase):
    """
    With ``CACHALOT_TENANT_SETTING`` unset, cachalot must behave exactly as it
    did before the feature existed, whatever the other settings say.
    """

    def test_key_functions_produce_only_the_legacy_key(self):
        legacy = [get_table_cache_key(DB, PARTITIONED)]
        for tenant in (None, '42', UNKNOWN):
            self.assertEqual(
                get_read_table_cache_keys(DB, PARTITIONED, tenant), legacy)
            self.assertEqual(
                get_write_table_cache_keys(DB, PARTITIONED, tenant), legacy)

    def test_every_table_counts_as_shared(self):
        self.assertTrue(are_all_shared({PARTITIONED}))
        self.assertTrue(are_all_shared({PARTITIONED, PLAIN}))


@override_settings(CACHALOT_TENANT_SETTING='app.tenant_id')
class TenantPlumbingTestCase(TenantStateMixin, TransactionTestCase):
    def tearDown(self):
        # Assert before resetting, so an unmatched push fails a test rather
        # than passing silently.
        stack = getattr(connection, '_cachalot_tenant_stack', None)
        self.assertFalse(stack, 'tenant stack leaked: %r' % (stack,))
        super().tearDown()

    def set_tenant(self, value):
        """Issue the statement the app would issue, through a real cursor.

        SQLite rejects it, PostgreSQL accepts it; either way the cursor patch
        observes it, which is what this test is about.
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute('SET LOCAL app.tenant_id = %s', [value])
        except Exception:
            pass

    def test_real_committed_nested_atomic_keeps_the_inner_tenant(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            with transaction.atomic():
                observe_statement(connection, "SET LOCAL app.tenant_id = '43'")
                self.assertEqual(get_tenant(connection), '43')
            self.assertEqual(get_tenant(connection), '43')
        self.assertIsNone(get_tenant(connection))

    def test_rolled_back_atomic_restores_outer_tenant(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            try:
                with transaction.atomic():
                    observe_statement(connection,
                                      "SET LOCAL app.tenant_id = '43'")
                    raise ValueError('rollback')
            except ValueError:
                pass
            self.assertEqual(get_tenant(connection), '42')

    def test_tenant_does_not_survive_the_transaction(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
        self.assertIsNone(get_tenant(connection))
        with transaction.atomic():
            self.assertIsNone(get_tenant(connection))

    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
    def test_cursor_observes_a_real_set_local(self):
        with transaction.atomic():
            self.set_tenant('42')
            self.assertEqual(get_tenant(connection), '42')

    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
    def test_cursor_observes_a_real_set_config(self):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT set_config(%s, %s, true)', ['app.tenant_id', '42'])
            self.assertEqual(get_tenant(connection), '42')

    @skipUnless(connection.vendor == 'sqlite',
                'SQLite only: relies on SET LOCAL syntax being rejected '
                'outright')
    def test_cursor_failure_lands_on_unknown(self):
        # SQLite rejects `SET LOCAL` outright, which is what drives the real
        # cursor through its failure path.
        with transaction.atomic():
            with self.assertRaises(Exception):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL app.tenant_id = '42'")
            self.assertIs(get_tenant(connection), UNKNOWN)

    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
    def test_cursor_failure_lands_on_unknown_postgresql(self):
        # PostgreSQL accepts any custom GUC, so a trailing token is what
        # makes the server reject it while the parser still reads the value.
        # Asserted first, or the failure below would prove nothing.
        sql = "SET LOCAL app.tenant_id = '42' GARBAGE"
        self.assertEqual(parse_tenant_statement(sql), '42')
        with transaction.atomic():
            with self.assertRaises(Exception):
                with connection.cursor() as cursor:
                    cursor.execute(sql)
            self.assertIs(get_tenant(connection), UNKNOWN)

    def test_stack_balances_when_feature_disabled_mid_transaction(self):
        # `pop_tenant` used to self-guard on `tenancy_enabled()`, so a block
        # pushed while the feature was on skipped its pop once it was off,
        # leaving the stack one deeper for good.
        override = override_settings(CACHALOT_TENANT_SETTING=None)
        try:
            with transaction.atomic():
                observe_statement(
                    connection, "SET LOCAL app.tenant_id = '42'")
                self.assertEqual(get_tenant(connection), '42')
                override.enable()
                self.assertIsNone(get_tenant(connection))
        finally:
            # Not `addCleanup`: the assertions below need the feature on
            # again, and a failure above must not leak the override.
            override.disable()
        stack = getattr(connection, '_cachalot_tenant_stack', None)
        self.assertFalse(stack, 'tenant stack leaked: %r' % (stack,))
        with transaction.atomic():
            self.assertIsNone(get_tenant(connection))

    def test_interrupted_statement_lands_on_unknown(self):
        # Only a BaseException pins the wider `except` clause down; an
        # OperationalError would pass against a plain `except Exception`.
        # psycopg2's cursor is a C type with a read-only `execute`, hence
        # the swap on Django's wrapper instead.
        class _RaisingCursor:
            def execute(self, *args, **kwargs):
                raise KeyboardInterrupt

        with transaction.atomic():
            with connection.cursor() as cursor:
                real_cursor = cursor.cursor
                cursor.cursor = _RaisingCursor()
                try:
                    with self.assertRaises(KeyboardInterrupt):
                        cursor.execute("SET LOCAL app.tenant_id = '42'")
                finally:
                    cursor.cursor = real_cursor
            self.assertIs(get_tenant(connection), UNKNOWN)


TENANCY = dict(CACHALOT_TENANT_SETTING='app.tenant_id',
               CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))


@contextmanager
def as_tenant(value):
    """Run a block inside a transaction with ``value`` as the active tenant."""
    with transaction.atomic():
        observe_statement(connection, 'SET LOCAL app.tenant_id = %s', [value])
        yield


@override_settings(**TENANCY)
class TenantInvalidationTestCase(TenantStateMixin, TestUtilsMixin,
                                 FilteredTransactionTestCase):
    def last(self, tenant=None):
        return get_last_invalidation(PARTITIONED, tenant=tenant)

    def test_scoped_write_bumps_any_and_tenant_keys(self):
        before_other = self.last('b')
        with as_tenant('a'):
            Test.objects.create(name='x')
        self.assertGreater(self.last('a'), 0.0)
        self.assertGreater(self.last(None), 0.0)
        self.assertEqual(self.last('b'), before_other)

    def test_unscoped_write_bumps_every_tenant(self):
        Test.objects.create(name='x')
        self.assertGreater(self.last('a'), 0.0)
        self.assertGreater(self.last('b'), 0.0)
        self.assertGreater(self.last(None), 0.0)

    def test_explicit_invalidate_defaults_to_global(self):
        with as_tenant('a'):
            invalidate(Test)
        self.assertGreater(self.last('b'), 0.0)

    def test_explicit_invalidate_can_be_narrowed(self):
        before_other = self.last('b')
        invalidate(Test, tenant='a')
        self.assertGreater(self.last('a'), 0.0)
        self.assertEqual(self.last('b'), before_other)

    def test_unknown_tenant_invalidates_globally(self):
        before_other = self.last('b')
        with transaction.atomic():
            observe_statement(connection, "SET app.tenant_id = 'a'")
            Test.objects.create(name='x')
        self.assertGreater(self.last('b'), before_other)

    def test_signal_carries_the_tenant(self):
        received = []

        def receiver(sender, **kwargs):
            received.append((sender, kwargs.get('tenant')))

        post_invalidation.connect(receiver)
        try:
            with as_tenant('a'):
                Test.objects.create(name='x')
        finally:
            post_invalidation.disconnect(receiver)
        self.assertIn((PARTITIONED, 'a'), received)

    def test_invalidate_with_unknown_tenant_behaves_like_global(self):
        invalidate(Test, tenant=UNKNOWN)
        self.assertGreater(self.last(None), 0.0)
        self.assertGreater(self.last('a'), 0.0)
        self.assertGreater(self.last('b'), 0.0)

        # Built by hand: the key function normalises UNKNOWN away.
        bogus_key = cachalot_settings.CACHALOT_TABLE_KEYGEN(
            DEFAULT_DB_ALIAS, PARTITIONED + TENANT_TABLE_SUFFIX + str(UNKNOWN))
        cache = cachalot_caches.get_cache(db_alias=DEFAULT_DB_ALIAS)
        self.assertEqual(cache.get_many([bogus_key]), {})

    def test_shared_table_write_signals_once_across_tenants(self):
        received = []

        def receiver(sender, **kwargs):
            received.append((sender, kwargs.get('tenant')))

        post_invalidation.connect(receiver)
        try:
            with transaction.atomic():
                with as_tenant('a'):
                    User.objects.create_user('user_a')
                with as_tenant('b'):
                    User.objects.create_user('user_b')
        finally:
            post_invalidation.disconnect(receiver)
        plain_signals = [pair for pair in received if pair[0] == PLAIN]
        self.assertEqual(plain_signals, [(PLAIN, None)])

    def test_partitioned_table_write_still_signals_once_per_tenant(self):
        received = []

        def receiver(sender, **kwargs):
            received.append((sender, kwargs.get('tenant')))

        post_invalidation.connect(receiver)
        try:
            with transaction.atomic():
                with as_tenant('a'):
                    Test.objects.create(name='x')
                with as_tenant('b'):
                    Test.objects.create(name='y')
        finally:
            post_invalidation.disconnect(receiver)
        partitioned_tenants = sorted(
            tenant for sender, tenant in received if sender == PARTITIONED)
        self.assertEqual(partitioned_tenants, ['a', 'b'])

    def test_get_last_invalidation_with_unknown_tenant_sees_scoped_write(self):
        with as_tenant('a'):
            Test.objects.create(name='x')
        expected = self.last(None)
        self.assertGreater(expected, 0.0)
        self.assertEqual(get_last_invalidation(PARTITIONED, tenant=UNKNOWN),
                         expected)


@override_settings(**TENANCY)
class PartitionedReadTestCase(TenantStateMixin, TestUtilsMixin,
                              FilteredTransactionTestCase):
    def read(self, tenant=None):
        if tenant is None:
            return list(Test.objects.all())
        with as_tenant(tenant):
            return list(Test.objects.all())

    def write(self, tenant=None, name='x'):
        if tenant is None:
            Test.objects.create(name=name)
        else:
            with as_tenant(tenant):
                Test.objects.create(name=name)

    def test_tenants_do_not_share_cached_results(self):
        with self.assertNumQueries(1):
            self.read('a')
        with self.assertNumQueries(1):
            self.read('b')
        with self.assertNumQueries(0):
            self.read('a')
        with self.assertNumQueries(0):
            self.read('b')

    def test_scoped_write_spares_another_tenant(self):
        self.read('a')
        self.read('b')
        self.write('a')
        with self.assertNumQueries(0):
            self.read('b')
        with self.assertNumQueries(1):
            self.read('a')

    def test_scoped_write_invalidates_unscoped_reads(self):
        with self.assertNumQueries(1):
            self.read()
        with self.assertNumQueries(0):
            self.read()
        self.write('a')
        with self.assertNumQueries(1):
            self.read()

    def test_unscoped_write_invalidates_every_tenant(self):
        self.read('a')
        self.read('b')
        self.write()
        with self.assertNumQueries(1):
            self.read('a')
        with self.assertNumQueries(1):
            self.read('b')

    def test_unscoped_and_scoped_reads_do_not_share(self):
        with self.assertNumQueries(1):
            self.read()
        with self.assertNumQueries(1):
            self.read('a')

    def test_unknown_tenant_is_never_cached(self):
        # A 1/1 query count also passes with caching off entirely, so it
        # proves nothing on its own. The cache is shared with other tests,
        # hence the before/after snapshot rather than an emptiness check.
        cache = cachalot_caches.get_cache(db_alias=DEFAULT_DB_ALIAS)
        keys = (
            get_write_table_cache_keys(DEFAULT_DB_ALIAS, PARTITIONED, None)
            + get_write_table_cache_keys(DEFAULT_DB_ALIAS, PARTITIONED, 'a'))
        before = cache.get_many(keys)
        with transaction.atomic():
            observe_statement(connection, "SET app.tenant_id = 'a'")
            with self.assertNumQueries(1):
                list(Test.objects.all())
            with self.assertNumQueries(1):
                list(Test.objects.all())
        after = cache.get_many(keys)
        self.assertEqual(after, before)


@override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                   CACHALOT_PARTITIONED_TABLES=(PARTITIONED,),
                   CACHALOT_TENANT_SHARED_TABLES=('cachalot_testparent',))
class SharedTableTestCase(TenantStateMixin, TestUtilsMixin,
                          FilteredTransactionTestCase):
    def test_shared_table_queries_are_reused_across_tenants(self):
        with as_tenant('a'):
            with self.assertNumQueries(1):
                list(TestParent.objects.all())
        with as_tenant('b'):
            with self.assertNumQueries(0):
                list(TestParent.objects.all())

    def test_non_partitioned_table_keeps_single_key_semantics(self):
        # auth_user is neither partitioned nor shared: its query key still
        # carries the tenant, but any write to it invalidates every tenant.
        with as_tenant('a'):
            with self.assertNumQueries(1):
                list(User.objects.all())
        with as_tenant('b'):
            with self.assertNumQueries(1):
                list(User.objects.all())
        with as_tenant('a'):
            with self.assertNumQueries(0):
                list(User.objects.all())
        with as_tenant('b'):
            User.objects.create_user('u1')
        with as_tenant('a'):
            with self.assertNumQueries(1):
                list(User.objects.all())

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=('cachalot_testparent',),
                       CACHALOT_TENANT_SHARED_TABLES=('cachalot_testparent',))
    def test_partitioned_and_shared_table_does_not_leak_across_tenants(self):
        # Listed as both, the query key used to stay unscoped while the
        # invalidation keys stayed per-tenant, so b's rows landed in the slot
        # a read from. a's last read is legitimately a cache hit; what it
        # returns is the point.
        with as_tenant('a'):
            with self.assertNumQueries(1):
                rows_a_before = list(TestParent.objects.all())
        with as_tenant('b'):
            TestParent.objects.create(name='from_b')
        with as_tenant('b'):
            with self.assertNumQueries(1):
                rows_b = list(TestParent.objects.all())
        with as_tenant('a'):
            with self.assertNumQueries(0):
                rows_a_after = list(TestParent.objects.all())
        self.assertEqual([row.name for row in rows_a_before], [])
        self.assertEqual([row.name for row in rows_b], ['from_b'])
        self.assertEqual([row.name for row in rows_a_after], [])


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
@override_settings(**TENANCY)
class PostgresTenancyTestCase(TenantStateMixin, TestUtilsMixin,
                              FilteredTransactionTestCase):
    """
    Drives the feature the way a real deployment does: the tenant arrives
    only as a PostgreSQL session variable, and an RLS policy - not the ORM -
    decides which rows a query sees.
    """

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT rolsuper OR rolbypassrls FROM pg_roles '
                           'WHERE rolname = current_user')
            if cursor.fetchone()[0]:
                self.skipTest('the database role bypasses Row-Level Security')
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute(
                'ALTER TABLE cachalot_test ADD COLUMN IF NOT EXISTS '
                'tenant_id text')
            cursor.execute('ALTER TABLE cachalot_test ENABLE ROW LEVEL '
                           'SECURITY')
            cursor.execute('ALTER TABLE cachalot_test FORCE ROW LEVEL '
                           'SECURITY')
            cursor.execute('DROP POLICY IF EXISTS cachalot_tenant_policy '
                           'ON cachalot_test')
            cursor.execute(
                'CREATE POLICY cachalot_tenant_policy ON cachalot_test '
                'USING (tenant_id IS NOT DISTINCT FROM '
                "current_setting('app.tenant_id', true)) "
                'WITH CHECK (true)')

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP POLICY IF EXISTS cachalot_tenant_policy '
                           'ON cachalot_test')
            cursor.execute('ALTER TABLE cachalot_test DISABLE ROW LEVEL '
                           'SECURITY')
            cursor.execute('ALTER TABLE cachalot_test DROP COLUMN IF EXISTS '
                           'tenant_id')
        super().tearDown()

    def set_tenant(self, cursor, value):
        cursor.execute('SELECT set_config(%s, %s, true)',
                       ['app.tenant_id', value])

    def db_tenant(self):
        """The tenant PostgreSQL itself is running under, right now."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.tenant_id', true)")
            return cursor.fetchone()[0]

    @contextmanager
    def tenant_transaction(self, tenant):
        """Open a transaction with ``tenant`` set, before any counting starts.

        The ``set_config`` call is a real SELECT and would otherwise be counted
        by ``assertNumQueries``, so it happens on entry rather than inside the
        block the caller measures.
        """
        with transaction.atomic():
            with connection.cursor() as cursor:
                self.set_tenant(cursor, tenant)
            yield

    def create(self, tenant, name):
        with self.tenant_transaction(tenant):
            with connection.cursor() as cursor:
                cursor.execute(
                    'INSERT INTO cachalot_test (name, public, tenant_id) '
                    'VALUES (%s, false, %s)', [name, tenant])

    def names(self, tenant):
        with self.tenant_transaction(tenant):
            return [t.name for t in Test.objects.all()]

    def test_tenants_see_only_their_own_rows_through_the_cache(self):
        self.create('a', 'row-a')
        self.create('b', 'row-b')
        self.assertEqual(self.names('a'), ['row-a'])
        self.assertEqual(self.names('b'), ['row-b'])
        self.assertEqual(self.names('a'), ['row-a'])
        self.assertEqual(self.names('b'), ['row-b'])

    def test_committed_nested_atomic_agrees_with_the_database(self):
        with self.tenant_transaction('a'):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    self.set_tenant(cursor, 'b')
            self.assertEqual(self.db_tenant(), 'b')
            self.assertEqual(get_tenant(connection), self.db_tenant())

    def test_rolled_back_nested_atomic_agrees_with_the_database(self):
        with self.tenant_transaction('a'):
            with self.assertRaises(ValueError):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        self.set_tenant(cursor, 'b')
                    raise ValueError('rollback')
            self.assertEqual(self.db_tenant(), 'a')
            self.assertEqual(get_tenant(connection), self.db_tenant())

    def test_committed_nested_atomic_does_not_leak_its_rows(self):
        # The outer block runs under tenant b from the nested block on, so
        # its read returns b's rows. Storing those under tenant a's key
        # served them straight back to an honest tenant-a transaction.
        self.create('a', 'row-a')
        self.create('b', 'row-b')
        with self.tenant_transaction('a'):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    self.set_tenant(cursor, 'b')
            self.assertEqual([t.name for t in Test.objects.all()], ['row-b'])
        self.assertEqual(self.names('a'), ['row-a'])

    def test_session_scoped_tenant_is_never_served_from_cache(self):
        self.create('a', 'row-a')
        self.create('b', 'row-b')
        with connection.cursor() as cursor:
            cursor.execute("SET app.tenant_id = 'a'")
        try:
            self.assertEqual([t.name for t in Test.objects.all()], ['row-a'])
            with connection.cursor() as cursor:
                cursor.execute("SET app.tenant_id = 'b'")
            self.assertEqual([t.name for t in Test.objects.all()], ['row-b'])
        finally:
            with connection.cursor() as cursor:
                cursor.execute('RESET app.tenant_id')

    def test_write_in_one_tenant_spares_the_other(self):
        self.create('a', 'row-a')
        self.create('b', 'row-b')
        self.names('a')
        self.names('b')
        self.create('a', 'row-a2')
        with self.tenant_transaction('a'):
            with self.assertNumQueries(1):
                self.assertEqual(sorted(t.name for t in Test.objects.all()),
                                 ['row-a', 'row-a2'])
        with self.tenant_transaction('b'):
            with self.assertNumQueries(0):
                self.assertEqual([t.name for t in Test.objects.all()],
                                 ['row-b'])


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
@override_settings(**TENANCY)
class PlaceholderOffsetTestCase(SimpleTestCase):
    """
    Check the parser's parameter mapping against the driver's own.

    Resolving `SET LOCAL app.tenant_id = %s` means counting the placeholders
    before it, and psycopg's rules for what counts are not obvious: a `%s`
    inside a string literal is one, `%%` is an escaped percent and is not,
    and psycopg3 spells a placeholder `%b` or `%t` as well.  Getting the
    count wrong resolves the tenant to some other parameter, which serves one
    tenant's rows to another without any error to notice.  So rather than
    pin a handful of cases by hand, ask the driver what it actually sent.
    """

    databases = {DEFAULT_DB_ALIAS}

    #: Fragments to build statements from, each with the number of
    #: placeholders psycopg2 will find in it.
    FRAGMENTS = (
        ("SELECT %s", 1),
        ("SELECT '%s'", 1),
        ("SELECT '100%% done'", 0),
        ("SELECT '%%s'", 0),
        ("SELECT %s, %s", 2),
        ("SELECT 'a', %s", 1),
        ("SELECT 1 /* %%s */", 0),
        ("SELECT 1 -- no placeholder here\n", 0),
        ("SELECT $$ %%s $$", 0),
        ("INSERT INTO log (msg) VALUES (%s)", 1),
        ("SELECT 'it''s %s'", 1),
    )

    def mogrify(self, sql, params):
        with connection.cursor() as cursor:
            sent = cursor.cursor.mogrify(sql, params)
        # psycopg2 returns bytes, psycopg3 str.
        return sent.decode() if isinstance(sent, bytes) else sent

    def driver_tenant(self, sql, params):
        """What the server will really receive as the tenant, per psycopg."""
        match = re.search(r"SET LOCAL app\.tenant_id = '((?:[^']|'')*)'",
                          self.mogrify(sql, params))
        self.assertIsNotNone(match, sql)
        return match.group(1).replace("''", "'")

    def test_parser_agrees_with_the_driver_on_every_prefix(self):
        for prefix, count in self.FRAGMENTS:
            sql = prefix + '; SET LOCAL app.tenant_id = %s'
            # Distinct values, so a miscount cannot land on the right one
            # by accident.
            params = ['decoy-%d' % i for i in range(count)] + ['the-tenant']
            self.assertEqual(parse_tenant_statement(sql, params),
                             self.driver_tenant(sql, params), sql)

    def test_parser_agrees_with_the_driver_on_random_statements(self):
        random = Random(20260829)
        for _ in range(300):
            parts = [random.choice(self.FRAGMENTS) for _ in range(3)]
            sql = '; '.join(part for part, _ in parts)
            sql += '; SET LOCAL app.tenant_id = %s'
            count = sum(n for _, n in parts)
            params = ['decoy-%d' % i for i in range(count)] + ['the-tenant']
            self.assertEqual(parse_tenant_statement(sql, params),
                             self.driver_tenant(sql, params), sql)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
@override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                   CACHALOT_PARTITIONED_TABLES=(PARTITIONED,),
                   CACHALOT_TENANT_SHARED_TABLES=('cachalot_testparent',))
class PostgresSharedTableTestCase(PostgresTenancyTestCase):
    """
    The shared-table opt-out, driven through real cursors.

    Every other shared-table test injects the tenant by calling
    ``observe_statement`` directly, so none of them exercises the cursor
    patch, the parser and the read path together.
    """

    def test_a_shared_table_is_read_once_for_every_tenant(self):
        TestParent.objects.create(name='shared')
        with self.tenant_transaction('a'):
            self.assertEqual([p.name for p in TestParent.objects.all()],
                             ['shared'])
        with self.tenant_transaction('b'):
            with self.assertNumQueries(0):
                self.assertEqual([p.name for p in TestParent.objects.all()],
                                 ['shared'])

    def test_a_partitioned_table_is_still_read_per_tenant(self):
        self.create('a', 'row-a')
        self.create('b', 'row-b')
        self.names('a')
        with self.tenant_transaction('b'):
            with self.assertNumQueries(1):
                self.assertEqual([t.name for t in Test.objects.all()],
                                 ['row-b'])

    def test_a_shared_read_in_a_distrusted_transaction_is_not_cached(self):
        TestParent.objects.create(name='shared')
        with transaction.atomic():
            with connection.cursor() as cursor:
                # PostgreSQL resolves the named parameter; cachalot cannot
                # map it back to the value that was sent.
                cursor.execute(
                    'SELECT set_config(%(name)s, %(value)s, true)',
                    {'name': 'app.tenant_id', 'value': 'a'})
            self.assertIs(get_tenant(connection), UNKNOWN)
            self.assertEqual([p.name for p in TestParent.objects.all()],
                             ['shared'])
        with self.tenant_transaction('a'):
            with self.assertNumQueries(1):
                self.assertEqual([p.name for p in TestParent.objects.all()],
                                 ['shared'])


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
@override_settings(**TENANCY)
class PostgresConcurrentTenantTestCase(PostgresTenancyTestCase):
    """
    Two tenants on two connections, sharing one cache.

    The tenant lives on the connection, and the cache does not, so a read
    cached by one thread is a candidate answer for the other.  Nothing else
    in the suite runs two tenants at once.
    """

    def run_as(self, tenant, body, results, index, barrier):
        try:
            with self.tenant_transaction(tenant):
                barrier.wait(timeout=30)
                results[index] = body()
        except Exception as error:      # noqa: BLE001 - reported by the caller
            results[index] = error
        finally:
            connection.close()

    def interleave(self, bodies):
        """Run ``bodies`` on their own connections, meeting at a barrier."""
        results = [None] * len(bodies)
        barrier = threading.Barrier(len(bodies))
        threads = [
            threading.Thread(target=self.run_as,
                             args=(tenant, body, results, index, barrier))
            for index, (tenant, body) in enumerate(bodies)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), 'thread did not finish')
        for result in results:
            if isinstance(result, Exception):
                raise result
        return results

    def read(self):
        return [t.name for t in Test.objects.all()]

    def test_simultaneous_reads_do_not_cross_tenants(self):
        self.create('a', 'row-a')
        self.create('b', 'row-b')
        self.assertEqual(
            self.interleave([('a', self.read), ('b', self.read)]),
            [['row-a'], ['row-b']])
        self.assertEqual(
            self.interleave([('a', self.read), ('b', self.read)]),
            [['row-a'], ['row-b']])

    def test_a_write_racing_a_read_does_not_cross_tenants(self):
        self.create('a', 'row-a')
        self.create('b', 'row-b')
        self.names('a')
        self.names('b')

        def write_b():
            with connection.cursor() as cursor:
                cursor.execute(
                    'INSERT INTO cachalot_test (name, public, tenant_id) '
                    "VALUES ('row-b2', false, 'b')")
            return sorted(self.read())

        self.assertEqual(
            self.interleave([('a', self.read), ('b', write_b)]),
            [['row-a'], ['row-b', 'row-b2']])
        self.assertEqual(self.names('a'), ['row-a'])
        self.assertEqual(sorted(self.names('b')), ['row-b', 'row-b2'])

    def test_the_tenant_does_not_follow_a_connection_into_another_thread(self):
        self.create('a', 'row-a')

        def tenantless_read():
            return get_tenant(connection)

        with self.tenant_transaction('a'):
            self.assertEqual(get_tenant(connection), 'a')
            other = []
            thread = threading.Thread(
                target=lambda: other.append(tenantless_read()))
            thread.start()
            thread.join(timeout=30)
        self.assertEqual(other, [None])


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
@override_settings(**TENANCY)
class PostgresReconnectTestCase(PostgresTenancyTestCase):
    def test_distrust_survives_a_reconnect(self):
        # PostgreSQL drops the session value when the connection goes, but
        # cachalot cannot see that happen, so it keeps distrusting rather
        # than guess. Pinned so a future change to it is a deliberate one.
        with connection.cursor() as cursor:
            cursor.execute("SET app.tenant_id = 'a'")
        self.assertIs(get_tenant(connection), UNKNOWN)
        connection.close()
        self.assertIsNone(self.db_tenant())
        self.assertIs(get_tenant(connection), UNKNOWN)

    def test_a_reset_after_a_reconnect_restores_trust(self):
        with connection.cursor() as cursor:
            cursor.execute("SET app.tenant_id = 'a'")
        connection.close()
        with connection.cursor() as cursor:
            cursor.execute('RESET app.tenant_id')
        self.assertIsNone(get_tenant(connection))
