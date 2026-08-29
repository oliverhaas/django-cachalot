from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.test import TransactionTestCase, override_settings, SimpleTestCase

from ..settings import cachalot_settings
from ..tenancy import (
    NOT_A_SET, UNKNOWN, are_all_shared, get_tenant, is_partitioned,
    observe_statement, parse_tenant_statement, pop_tenant, push_tenant,
)
from ..utils import (
    get_read_table_cache_keys, get_table_cache_key,
    get_tenant_query_cache_key, get_write_table_cache_keys,
)


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
