from contextlib import contextmanager
from unittest import mock, skipUnless

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
        with transaction.atomic():
            with connection.cursor() as cursor:
                with mock.patch.object(cursor.cursor, 'execute',
                                       side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        cursor.execute("SET LOCAL app.tenant_id = '42'")
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
        with transaction.atomic():
            observe_statement(connection, "SET app.tenant_id = 'a'")
            with self.assertNumQueries(1):
                list(Test.objects.all())
            with self.assertNumQueries(1):
                list(Test.objects.all())


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
        with as_tenant('a'):
            with self.assertNumQueries(0):
                list(User.objects.all())
        with as_tenant('b'):
            User.objects.create_user('u1')
        with as_tenant('a'):
            with self.assertNumQueries(1):
                list(User.objects.all())
