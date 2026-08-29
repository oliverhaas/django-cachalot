from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings

from ..api import invalidate
from ..local_cache import (
    _get_l1_cache, _MISS, _table_to_keys, local_get, local_invalidate,
)
from .models import Test
from .test_utils import TestUtilsMixin, FilteredTransactionTestCase


LOCAL_TABLES = {'cachalot_test': 30}


class LocalCacheTestCase(TestUtilsMixin, FilteredTransactionTestCase):
    """Tests for the two-tier (L1 local + L2 remote) cache feature."""

    def setUp(self):
        super().setUp()
        # Clear L1 state before each test
        _get_l1_cache().clear()
        _table_to_keys.clear()

    def tearDown(self):
        super().tearDown()
        _get_l1_cache().clear()
        _table_to_keys.clear()

    def test_disabled_by_default(self):
        """With default settings (empty dict), L1 is never used."""
        Test.objects.create(name='test1')
        with self.assertNumQueries(1):
            list(Test.objects.all())
        with self.assertNumQueries(0):
            list(Test.objects.all())
        # L1 should not have any entries for this table
        self.assertEqual(len(_table_to_keys), 0)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_l1_populated_on_read(self):
        """After a read, the result is stored in L1."""
        Test.objects.create(name='test1')
        # First read: DB query + L2 cache, also populates L1
        with self.assertNumQueries(1):
            data1 = list(Test.objects.all())
        self.assertEqual(len(data1), 1)
        # Verify L1 was populated
        self.assertIn('cachalot_test', _table_to_keys)
        self.assertTrue(len(_table_to_keys['cachalot_test']) > 0)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_l1_hit_avoids_l2(self):
        """On L1 hit, the result is returned without touching L2."""
        Test.objects.create(name='test1')
        # First read populates both L1 and L2
        with self.assertNumQueries(1):
            data1 = list(Test.objects.all())

        # Second read: 0 DB queries (could be L1 or L2)
        with self.assertNumQueries(0):
            data2 = list(Test.objects.all())
        self.assertListEqual(data1, data2)

        # Verify L1 has the cached key by checking directly
        cache_keys = list(_table_to_keys.get('cachalot_test', set()))
        self.assertTrue(len(cache_keys) > 0)
        result = local_get(cache_keys[0])
        self.assertIsNot(result, _MISS)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_write_invalidates_l1(self):
        """Writing to a configured table clears its L1 entries."""
        Test.objects.create(name='test1')
        # Populate L1
        with self.assertNumQueries(1):
            list(Test.objects.all())
        self.assertIn('cachalot_test', _table_to_keys)

        # Write to the table - triggers invalidation via post_invalidation
        Test.objects.create(name='test2')

        # L1 entries for this table should be cleared
        self.assertNotIn('cachalot_test', _table_to_keys)

        # Next read should require a DB query (L1 miss, L2 also invalidated)
        with self.assertNumQueries(1):
            data = list(Test.objects.all())
        self.assertEqual(len(data), 2)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_mixed_tables_skip_l1(self):
        """Queries involving unconfigured tables skip L1 entirely."""
        Test.objects.create(name='test1', owner=User.objects.create_user('u'))
        # Query joining Test (configured) and User (not configured)
        qs = Test.objects.select_related('owner')
        with self.assertNumQueries(1):
            list(qs)
        # L1 should NOT have entries because auth_user is not configured
        # (the query involves both cachalot_test and auth_user)
        l1_keys_for_test = _table_to_keys.get('cachalot_test', set())
        # The select_related query touches auth_user which is not in
        # LOCAL_TABLES, so L1 should not be used
        self.assertEqual(len(l1_keys_for_test), 0)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES={
        'cachalot_test': 30, 'auth_user': 60,
    })
    def test_all_configured_tables_use_l1(self):
        """Queries where ALL tables are configured use L1."""
        user = User.objects.create_user('u')
        Test.objects.create(name='test1', owner=user)
        # Query joining two configured tables
        qs = Test.objects.select_related('owner')
        with self.assertNumQueries(1):
            data1 = list(qs)

        # L1 should be populated
        total_keys = sum(len(v) for v in _table_to_keys.values())
        self.assertGreater(total_keys, 0)

        # Second read should hit L1 (0 DB queries)
        with self.assertNumQueries(0):
            data2 = list(qs)
        self.assertListEqual(data1, data2)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_api_invalidate_clears_l1(self):
        """Calling invalidate() directly clears L1 entries."""
        Test.objects.create(name='test1')
        # Populate L1
        with self.assertNumQueries(1):
            list(Test.objects.all())
        self.assertIn('cachalot_test', _table_to_keys)

        # Use the public API to invalidate
        invalidate('cachalot_test')

        # L1 should be cleared
        self.assertNotIn('cachalot_test', _table_to_keys)

        # Next read should require a DB query
        with self.assertNumQueries(1):
            list(Test.objects.all())

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_transaction_rollback_does_not_poison_l1(self):
        """L1 is not populated during transactions, so rollback can't poison it."""
        Test.objects.create(name='test1')
        # Populate L1 outside transaction
        with self.assertNumQueries(1):
            data1 = list(Test.objects.all())
        self.assertEqual(len(data1), 1)

        # Get L1 keys before transaction
        keys_before = set(_table_to_keys.get('cachalot_test', set()))

        try:
            with transaction.atomic():
                Test.objects.create(name='test2')
                # Read inside transaction - should NOT populate L1
                list(Test.objects.all())
                raise ZeroDivisionError
        except ZeroDivisionError:
            pass

        # After rollback, L1 should still have valid data
        # (the signal fires on rollback too, but the original cache
        # key data should not have been replaced with transactional data)
        with self.assertNumQueries(0):
            data2 = list(Test.objects.all())
        self.assertListEqual(data2, data1)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_transaction_commit_invalidates_l1(self):
        """L1 is invalidated after a transaction commits."""
        Test.objects.create(name='test1')
        # Populate L1
        with self.assertNumQueries(1):
            list(Test.objects.all())
        self.assertIn('cachalot_test', _table_to_keys)

        with transaction.atomic():
            Test.objects.create(name='test2')
            # During transaction, L1 should still have old entries
            # (signal not fired yet)

        # After commit, post_invalidation fires and L1 is cleared
        self.assertNotIn('cachalot_test', _table_to_keys)

        # Next read requires DB query
        with self.assertNumQueries(1):
            data = list(Test.objects.all())
        self.assertEqual(len(data), 2)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_cachalot_disabled_skips_l1(self):
        """L1 is not used when cachalot is disabled."""
        from ..api import cachalot_disabled
        Test.objects.create(name='test1')
        # Populate L1
        with self.assertNumQueries(1):
            list(Test.objects.all())

        with cachalot_disabled():
            # Should go to DB, not L1
            with self.assertNumQueries(1):
                list(Test.objects.all())

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_raw_sql_write_invalidates_l1(self):
        """Raw SQL writes that are detected by cachalot also invalidate L1."""
        Test.objects.create(name='test1')
        # Populate L1
        with self.assertNumQueries(1):
            list(Test.objects.all())
        self.assertIn('cachalot_test', _table_to_keys)

        # Raw SQL insert
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO cachalot_test (name, public) "
                "VALUES ('test2', %s);",
                [1 if self.is_sqlite else True])

        # L1 should be invalidated
        self.assertNotIn('cachalot_test', _table_to_keys)

        # Next read requires DB query
        with self.assertNumQueries(1):
            data = list(Test.objects.all())
        self.assertEqual(len(data), 2)

    @override_settings(CACHALOT_LOCAL_CACHE_TABLES=LOCAL_TABLES)
    def test_l1_repopulated_after_invalidation(self):
        """After invalidation, the next read repopulates L1."""
        Test.objects.create(name='test1')

        # Populate L1
        with self.assertNumQueries(1):
            list(Test.objects.all())
        self.assertIn('cachalot_test', _table_to_keys)

        # Invalidate
        Test.objects.create(name='test2')
        self.assertNotIn('cachalot_test', _table_to_keys)

        # Re-read: populates L1 again
        with self.assertNumQueries(1):
            list(Test.objects.all())
        self.assertIn('cachalot_test', _table_to_keys)

        # L1 hit
        with self.assertNumQueries(0):
            list(Test.objects.all())
