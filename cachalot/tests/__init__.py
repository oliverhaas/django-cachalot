from django.core.signals import setting_changed
from django.dispatch import receiver

from ..settings import cachalot_settings
from .read import ReadTestCase, ParameterTypeTestCase
from .write import WriteTestCase, DatabaseCommandTestCase
from .transaction import AtomicCacheTestCase, AtomicTestCase
from .thread_safety import ThreadSafetyTestCase
from .multi_db import MultiDatabaseTestCase
from .settings import SettingsTestCase
from .api import APITestCase, CommandTestCase
from .signals import SignalsTestCase
from .postgres import PostgresReadTestCase
from .debug_toolbar import DebugToolbarTestCase
from .tenancy import (
    ConnectionTenantTestCase, DisabledFeatureTestCase,
    ParseTenantStatementTestCase, PartitionedReadTestCase,
    PlaceholderOffsetTestCase, PostgresConcurrentTenantTestCase,
    PostgresReconnectTestCase, PostgresSharedTableTestCase,
    PostgresTenancyTestCase, SharedTableTestCase, TableCacheKeysTestCase,
    TablePredicatesTestCase, TenancyEnabledTestCase, TenancySettingsTestCase,
    TenantInvalidationTestCase, TenantPlumbingTestCase,
)


@receiver(setting_changed)
def reload_settings(sender, **kwargs):
    cachalot_settings.reload()
