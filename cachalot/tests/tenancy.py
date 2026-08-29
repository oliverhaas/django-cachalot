from django.test import TransactionTestCase, override_settings, SimpleTestCase

from ..settings import cachalot_settings
from ..tenancy import NOT_A_SET, UNKNOWN, parse_tenant_statement


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
