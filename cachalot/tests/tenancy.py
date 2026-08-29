from contextlib import contextmanager
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
        self.assertEqual(self.parse('SET LOCAL "app.tenant_id" = \'42\''), '42')

    def test_set_local_placeholder(self):
        self.assertEqual(self.parse('SET LOCAL app.tenant_id = %s', ['42']),
                         '42')
        self.assertEqual(self.parse('SET LOCAL app.tenant_id = %s', [42]), '42')

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
        # pyformat placeholders
        self.assertIs(
            self.parse('SET LOCAL app.tenant_id = %(t)s', {'t': '42'}), UNKNOWN)
        # a computed value we will not evaluate
        self.assertIs(self.parse('SET LOCAL app.tenant_id = current_user'),
                      UNKNOWN)
        # the locality flag itself is a placeholder
        self.assertIs(
            self.parse("SELECT set_config('app.tenant_id', '42', %s)", [True]),
            UNKNOWN)
        # a placeholder with no matching parameter
        self.assertIs(self.parse('SET LOCAL app.tenant_id = %s', []), UNKNOWN)

    def test_disabled_feature_parses_nothing(self):
        with override_settings(CACHALOT_TENANT_SETTING=None):
            self.assertIs(self.parse("SET LOCAL app.tenant_id = '42'"),
                          NOT_A_SET)

    def test_multiple_constructs_fail_closed(self):
        # Two SET LOCAL statements for the GUC.
        self.assertIs(
            self.parse("SET LOCAL app.tenant_id = '1'; SET LOCAL app.tenant_id = '2'"),
            UNKNOWN)
        # SET LOCAL followed by RESET of the GUC.
        self.assertIs(
            self.parse("SET LOCAL app.tenant_id = '42'; RESET app.tenant_id"),
            UNKNOWN)

    def test_unrelated_trailing_statement_ignored(self):
        # One SET LOCAL for the GUC plus an unrelated statement.
        # The guard does not fire; we return the value.
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
class ConnectionTenantTestCase(TransactionTestCase):
    def tearDown(self):
        connection._cachalot_tenant = None
        connection._cachalot_tenant_stack = []

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

    def test_nested_atomic_restores_outer_tenant(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            push_tenant(connection)
            observe_statement(connection, "SET LOCAL app.tenant_id = '43'")
            self.assertEqual(get_tenant(connection), '43')
            pop_tenant(connection)
            self.assertEqual(get_tenant(connection), '42')

    def test_pop_without_push_clears(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            pop_tenant(connection)
            self.assertIsNone(get_tenant(connection))

    def test_disabled_feature_records_nothing(self):
        with override_settings(CACHALOT_TENANT_SETTING=None):
            with transaction.atomic():
                observe_statement(connection,
                                  "SET LOCAL app.tenant_id = '42'")
                self.assertIsNone(get_tenant(connection))

    def test_outermost_push_clears_dirty_connection(self):
        with transaction.atomic():
            # Simulate a missed pop by manually setting _cachalot_tenant
            # with an empty stack, as if a previous transaction leaked
            connection._cachalot_tenant = '99'
            connection._cachalot_tenant_stack = []
            push_tenant(connection)
            self.assertIsNone(get_tenant(connection))

    def test_nested_push_does_not_clear(self):
        with transaction.atomic():
            # Outermost push clears the live value
            push_tenant(connection)
            # Set tenant inside the outer block
            observe_statement(connection, "SET LOCAL app.tenant_id = '7'")
            # Guard: prove the setup worked
            self.assertEqual(get_tenant(connection), '7')
            # Nested push must not clear the live value
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
        # With the feature off, are_all_shared should return True to behave
        # like master (no tenant scoping), even if the table is not in the
        # shared tables list.
        self.assertTrue(are_all_shared({'cachalot_test'}))


DB = DEFAULT_DB_ALIAS
PARTITIONED = 'cachalot_test'
PLAIN = 'auth_user'


class TableCacheKeysTestCase(SimpleTestCase):
    def legacy_key(self, table):
        return get_table_cache_key(DB, table)

    def test_disabled_feature_produces_legacy_keys(self):
        for tenant in (None, '42'):
            self.assertEqual(get_read_table_cache_keys(DB, PARTITIONED, tenant),
                             [self.legacy_key(PARTITIONED)])
            self.assertEqual(get_write_table_cache_keys(DB, PARTITIONED, tenant),
                             [self.legacy_key(PARTITIONED)])

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))
    def test_partitioned_table_keys(self):
        k_any = self.legacy_key(PARTITIONED)
        read_unscoped = get_read_table_cache_keys(DB, PARTITIONED, None)
        read_scoped = get_read_table_cache_keys(DB, PARTITIONED, '42')
        write_unscoped = get_write_table_cache_keys(DB, PARTITIONED, None)
        write_scoped = get_write_table_cache_keys(DB, PARTITIONED, '42')

        # K_any stays byte-identical to the pre-feature key.
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
        # A tenant value of 42 (int) and '42' (str) must fold to the same
        # keys, since callers may pass either before normalisation.
        self.assertEqual(get_read_table_cache_keys(DB, PARTITIONED, 42),
                         get_read_table_cache_keys(DB, PARTITIONED, '42'))
        self.assertEqual(get_write_table_cache_keys(DB, PARTITIONED, 42),
                         get_write_table_cache_keys(DB, PARTITIONED, '42'))

    @override_settings(CACHALOT_TENANT_SETTING='app.tenant_id',
                       CACHALOT_PARTITIONED_TABLES=(PARTITIONED,))
    def test_unknown_tenant_normalised_to_none_in_key_functions(self):
        # UNKNOWN must fold to the same single legacy key as an unscoped
        # (None) tenant, not derive a key from the sentinel's repr.
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


@override_settings(CACHALOT_TENANT_SETTING='app.tenant_id')
class TenantPlumbingTestCase(TransactionTestCase):
    def tearDown(self):
        # A non-empty stack here means some push was never matched by a pop.
        # Assert it before resetting, so an imbalance fails a test instead of
        # passing silently.
        stack = getattr(connection, '_cachalot_tenant_stack', None)
        self.assertFalse(stack, 'tenant stack leaked: %r' % (stack,))
        connection._cachalot_tenant = None
        connection._cachalot_tenant_stack = []

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

    def test_real_nested_atomic_restores_outer_tenant(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            with transaction.atomic():
                observe_statement(connection, "SET LOCAL app.tenant_id = '43'")
                self.assertEqual(get_tenant(connection), '43')
            self.assertEqual(get_tenant(connection), '42')
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
        # SQLite rejects `SET LOCAL` syntax outright, which drives the real
        # patched cursor through its `except BaseException` path: the error
        # must still propagate, and the tenant must land on UNKNOWN rather
        # than being left alone or silently swallowed.
        with transaction.atomic():
            with self.assertRaises(Exception):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL app.tenant_id = '42'")
            self.assertIs(get_tenant(connection), UNKNOWN)

    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
    def test_cursor_failure_lands_on_unknown_postgresql(self):
        # PostgreSQL accepts a custom GUC happily, so a bare `SET LOCAL`
        # never fails there the way it does on SQLite. A trailing token
        # after a valid-looking assignment still fails on the server with a
        # syntax error, while the regex that extracts the tenant value pays
        # it no mind, since it does not require the match to reach the end
        # of the statement.
        sql = "SET LOCAL app.tenant_id = '42' GARBAGE"
        # Confirm the statement is recognised as touching the GUC and a
        # value extracted from it. Without this, the statement would be
        # ignored as NOT_A_SET and the failure below would prove nothing.
        self.assertEqual(parse_tenant_statement(sql), '42')
        with transaction.atomic():
            with self.assertRaises(Exception):
                with connection.cursor() as cursor:
                    cursor.execute(sql)
            self.assertIs(get_tenant(connection), UNKNOWN)

    def test_stack_balances_when_feature_disabled_before_transaction_exits(self):
        # Toggling CACHALOT_TENANT_SETTING off mid-transaction used to leak:
        # `pop_tenant` self-guarded on `tenancy_enabled()`, so a block pushed
        # while the feature was on would skip its pop if the feature was off
        # by the time the block exited, leaving the stack permanently one
        # deeper and letting a stale tenant survive into later transactions.
        override = override_settings(CACHALOT_TENANT_SETTING=None)
        try:
            with transaction.atomic():
                observe_statement(
                    connection, "SET LOCAL app.tenant_id = '42'")
                self.assertEqual(get_tenant(connection), '42')
                override.enable()
                # The feature is off from here on, including when this
                # `transaction.atomic()` block's `__exit__` runs below -
                # exactly the case `pop_tenant` must still handle correctly.
                self.assertIsNone(get_tenant(connection))
        finally:
            # Not `addCleanup`: the assertions below need the feature back on.
            # Without the `finally` a failed assertion above would skip this
            # and leak the override into every later test in the process.
            override.disable()
        stack = getattr(connection, '_cachalot_tenant_stack', None)
        self.assertFalse(stack, 'tenant stack leaked: %r' % (stack,))
        with transaction.atomic():
            self.assertIsNone(get_tenant(connection))

    def test_interrupted_statement_lands_on_unknown(self):
        # `except BaseException`, not `except Exception`: a statement killed
        # by KeyboardInterrupt or SystemExit did not take effect either, so
        # its tenant must not be trusted.  A plain OperationalError would
        # pass against the old `except Exception` too, so this is the only
        # test that pins the wider clause down.
        #
        # The raw DB-API cursor cannot be mocked directly: psycopg2's cursor
        # is a C extension type whose `execute` attribute is read-only, so
        # `mock.patch.object` on it raises `AttributeError` instead of
        # patching. Substituting the `cursor` attribute on the Django
        # `CursorWrapper` instance itself works identically on every
        # backend, since `CursorWrapper` is a plain Python object.
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
class TenantInvalidationTestCase(TestUtilsMixin, FilteredTransactionTestCase):
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
            # A non-LOCAL SET is unparsable, so the tenant becomes UNKNOWN.
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

        # No pseudo-tenant key was minted for the sentinel's repr. Built by
        # hand, bypassing get_write_table_cache_keys, since that function now
        # normalises UNKNOWN itself and can no longer produce this key.
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
class PartitionedReadTestCase(TestUtilsMixin, FilteredTransactionTestCase):
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
        # Assert on the cache directly, not just on the query count: a 1/1
        # sequence also passes with caching switched off entirely, so it does
        # not by itself prove the read stored anything. Snapshot the table
        # cache keys this table could plausibly be stored under (unscoped or
        # under tenant 'a') before and after, since the cache is shared with
        # other tests and may already hold unrelated entries for this table.
        cache = cachalot_caches.get_cache(db_alias=DEFAULT_DB_ALIAS)
        keys = (get_write_table_cache_keys(DEFAULT_DB_ALIAS, PARTITIONED, None)
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
class SharedTableTestCase(TestUtilsMixin, FilteredTransactionTestCase):
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
            # Distinct tenant, so this must not be served from tenant a's
            # cache entry even though the table itself is not partitioned.
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
        # A table listed as both partitioned and shared used to leak: the
        # query key stayed unscoped (are_all_shared wrongly said True) while
        # the invalidation keys stayed per-tenant, so tenant b's write only
        # bumped its own tenant key, and b's re-stored rows -- freshly
        # written under that same unscoped query key when b's own read
        # missed -- were then served straight back to tenant a on a's next,
        # otherwise valid, cache hit.
        #
        # With the fix, a's and b's reads land on distinct, tenant-folded
        # query keys, so there is no shared slot left for b's rows to leak
        # through. Tenant a's own key is never touched by b's write (that is
        # the point of partitioning: one tenant's write must not invalidate
        # another tenant's cache), so a's final read is legitimately a cache
        # hit (0 queries) too -- what must not happen is that hit returning
        # b's row, which is what the assertions below pin down.
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
class PostgresTenancyTestCase(TestUtilsMixin, FilteredTransactionTestCase):
    """
    Drives the feature the way a real deployment does: the tenant arrives
    only as a PostgreSQL session variable, and an RLS policy - not the ORM -
    decides which rows a query sees.
    """

    def setUp(self):
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
        # Served from cache this time, and still not each other's rows.
        self.assertEqual(self.names('a'), ['row-a'])
        self.assertEqual(self.names('b'), ['row-b'])

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
