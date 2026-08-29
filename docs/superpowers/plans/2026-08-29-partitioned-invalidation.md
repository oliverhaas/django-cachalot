# Partitioned Tenant-Scoped Invalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let django-cachalot invalidate cached queries per tenant instead of per table, and stop two tenants from colliding on one query cache key under PostgreSQL Row-Level Security.

**Architecture:** A new `cachalot/tenancy.py` learns the current tenant by sniffing `SET LOCAL` / `set_config()` statements as they pass through the already-patched cursor, and stores it on the Django connection, scoped to the transaction via the already-patched `Atomic`. Each participating table gains three invalidation keys (any-write / global / per-tenant) chosen so reads and writes touch two keys and tenants are never enumerated. The active tenant is also folded into the query cache key.

**Tech Stack:** Python 3.9+, Django 4.2–6.1, PostgreSQL (RLS), stdlib `re` and `hashlib`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-29-partitioned-invalidation-design.md` — read it before Task 1. The correctness matrix in its "Component: key scheme" section is the thing every test in this plan is ultimately checking.

## Global Constraints

- **No new dependencies.** Everything is stdlib or already imported by cachalot.
- **`CACHALOT_TENANT_SETTING = None` (the default) must be a total no-op.** Cache keys byte-identical to `master`, cursor sniffing not installed, no new work on any hot path. Task 8 tests this explicitly.
- **Fail closed, always.** When the tenant cannot be determined the value is the `UNKNOWN` sentinel, which means: reads are not cached at all, and writes invalidate globally. Never guess a tenant, never retain a stale one.
- **`K_any` must stay byte-identical to today's `get_table_cache_key(db_alias, table)`.** This is what keeps partitioning-unaware code paths (custom `CACHALOT_TABLE_KEYGEN`, third-party `invalidate()` callers, `post_migrate`) correct.
- **All new table keys go through `cachalot_settings.CACHALOT_TABLE_KEYGEN`**, never a bare `sha1()`, so custom keygens keep working.
- **Tenant values are normalised to `str`** before entering any cache key. `42` and `'42'` must produce the same key.
- Follow existing repo style: 4-space indent, single quotes, no type annotations in `cachalot/` outside `TYPE_CHECKING` blocks.
- Tasks 2 onward say "append to `cachalot/tests/tenancy.py`" and show the imports each block needs. Put those imports at the **top** of the module with the existing ones rather than mid-file, and drop any that a later task makes redundant.

## Test commands

Fast loop (SQLite only, no servers needed — created in Task 1):

```bash
source .venv/bin/activate
DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2
```

Full suite on SQLite:

```bash
source .venv/bin/activate
DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests --noinput -v1
```

The default `settings` module declares PostgreSQL, MySQL, Redis and Memcached and requires all four to be running; `runtests.py` uses it. Use it only when those servers are up.

**The baseline is green: 198 tests, 0 failures, 23 skipped.** It was verified on this branch before Task 1. Any failure you see is yours — do not attribute it to a pre-existing problem.

## File Structure

| File | Responsibility |
|---|---|
| `cachalot/tenancy.py` | **new.** Knows the current tenant of a connection, and which tables are partitioned or shared. Owns the GUC parser and the connection-state helpers. Knows nothing about cache keys. |
| `cachalot/settings.py` | Four new settings and their converters. |
| `cachalot/utils.py` | Partitioned key generation; `_get_table_cache_keys` returns `(tables, keys)`; `_invalidate_tables` takes a tenant. |
| `cachalot/monkey_patch.py` | Wiring only: cursor sniffs GUCs, atomic pushes/pops tenant, compiler folds tenant into the query key, write compiler passes the ambient tenant. |
| `cachalot/transaction.py` | `to_be_invalidated` holds `(table, tenant)` pairs. |
| `cachalot/cache.py` | `exit_atomic` emits one signal per `(table, tenant)`. |
| `cachalot/api.py` | `tenant` kwarg on `invalidate()` and `get_last_invalidation()`. |
| `cachalot/tests/tenancy.py` | **new.** All tests for this feature. |
| `test_settings_sqlite.py` | **new.** SQLite + locmem settings for the fast loop. |
| `docs/tenancy.rst` | **new.** User-facing documentation. |

---

### Task 1: Settings and the SQLite test harness

**Files:**
- Create: `test_settings_sqlite.py`
- Modify: `cachalot/settings.py`
- Create: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `cachalot_settings.CACHALOT_TENANT_SETTING` (`str | None`), `.CACHALOT_PARTITIONED_TABLES` (`frozenset[str]`), `.CACHALOT_TENANT_SHARED_TABLES` (`frozenset[str]`), `.CACHALOT_PARTITIONED_APPS` (`tuple`).

- [ ] **Step 1: Create the SQLite settings module**

Create `test_settings_sqlite.py` at the repo root:

```python
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
```

- [ ] **Step 2: Verify the harness runs an existing test module**

Run: `source .venv/bin/activate && DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.signals --noinput -v1`
Expected: `OK`. Then run the whole suite — `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests --noinput -v1` — and confirm `OK (skipped=23)` over 198 tests.

- [ ] **Step 3: Write the failing settings test**

Create `cachalot/tests/tenancy.py`:

```python
from django.test import TransactionTestCase, override_settings

from ..settings import cachalot_settings


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
```

Register it by adding to `cachalot/tests/__init__.py`, after the `from .debug_toolbar import DebugToolbarTestCase` line:

```python
from .tenancy import TenancySettingsTestCase
```

- [ ] **Step 4: Run it to confirm it fails**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'CACHALOT_TENANT_SETTING'`.

- [ ] **Step 5: Add the settings**

In `cachalot/settings.py`, inside `class Settings`, after the `CACHALOT_FINAL_SQL_CHECK = False` line:

```python
    CACHALOT_TENANT_SETTING = None
    CACHALOT_PARTITIONED_TABLES = ()
    CACHALOT_PARTITIONED_APPS = ()
    CACHALOT_TENANT_SHARED_TABLES = ()
```

At the end of the file, before `cachalot_settings = Settings()`, add the converters:

```python
@Settings.add_converter('CACHALOT_PARTITIONED_TABLES')
def convert(value):
    return convert_tables(value, 'CACHALOT_PARTITIONED_APPS')


@Settings.add_converter('CACHALOT_TENANT_SHARED_TABLES')
def convert(value):
    return frozenset(value)
```

`convert_tables()` already exists in this module and expands the `_APPS` companion setting into table names; this is the same pattern `CACHALOT_ONLY_CACHABLE_TABLES` uses.

- [ ] **Step 6: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS — every test in `cachalot.tests.tenancy`.

- [ ] **Step 7: Commit**

```bash
git add test_settings_sqlite.py cachalot/settings.py cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "feat: Add tenancy settings and a SQLite-only test harness"
```

---

### Task 2: The GUC statement parser

A pure function, no Django state, testable on any backend. It answers one question about one SQL statement: did this set the tenant GUC, and to what?

**Files:**
- Create: `cachalot/tenancy.py`
- Modify: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: `cachalot_settings.CACHALOT_TENANT_SETTING` from Task 1.
- Produces:
  - `NOT_A_SET`, `UNKNOWN` — module-level sentinels, importable from `cachalot.tenancy`.
  - `tenancy_enabled() -> bool`
  - `parse_tenant_statement(sql, params) -> NOT_A_SET | UNKNOWN | str | None` — `NOT_A_SET` means the statement did not touch the GUC; `None` means the GUC was cleared; a `str` is the new tenant; `UNKNOWN` means it was touched in a way we refuse to interpret.

- [ ] **Step 1: Write the failing parser tests**

Append to `cachalot/tests/tenancy.py`:

```python
from django.test import SimpleTestCase

from ..tenancy import NOT_A_SET, UNKNOWN, parse_tenant_statement


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
```

Add to the import line in `cachalot/tests/__init__.py`:

```python
from .tenancy import ParseTenantStatementTestCase, TenancySettingsTestCase
```

- [ ] **Step 2: Run to confirm it fails**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: FAIL — `ModuleNotFoundError: No module named 'cachalot.tenancy'`.

- [ ] **Step 3: Write the parser**

Create `cachalot/tenancy.py`:

```python
import re
from functools import lru_cache

from .settings import cachalot_settings


class _Sentinel:
    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return '<cachalot %s>' % self.name


#: The statement did not touch the tenant setting.
NOT_A_SET = _Sentinel('NOT_A_SET')
#: The tenant cannot be determined; callers must fail closed.
UNKNOWN = _Sentinel('UNKNOWN')

# Only a numeric literal is accepted unquoted. A bare identifier such as
# `current_user` may be a function call, so it fails closed to UNKNOWN.
_UNQUOTED_NUMBER_RE = re.compile(r'\A[-+]?\d+(?:\.\d+)?\Z')


def tenancy_enabled():
    return cachalot_settings.CACHALOT_TENANT_SETTING is not None


@lru_cache(maxsize=8)
def _compile(guc):
    name = re.escape(guc)
    return (
        # SET [LOCAL|SESSION] <guc> {=|TO} <value>
        re.compile(
            r'\bSET\s+(?:(?P<scope>LOCAL|SESSION)\s+)?"?%s"?\s*'
            r'(?:=|\bTO\b)\s*(?P<value>\'(?:[^\']|\'\')*\'|[^\s;,)]+)' % name,
            re.IGNORECASE),
        # RESET <guc>
        re.compile(r'\bRESET\s+"?%s"?' % name, re.IGNORECASE),
        # set_config(<name>, <value>, <is_local>)
        re.compile(
            r'\bset_config\s*\(\s*'
            r"(?P<name>'(?:[^']|'')*'|%s)\s*,\s*"
            r"(?P<value>'(?:[^']|'')*'|%s|NULL)\s*,\s*"
            r'(?P<local>[^\s,)]+)\s*\)',
            re.IGNORECASE),
    )


#: The opening (or closing) delimiter of a dollar-quoted string, tag included.
_DOLLAR_TAG_RE = re.compile(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$')

#: Characters that may precede the ``E`` of an ``E'...'`` string only if it is
#: not simply the tail of an identifier or keyword (``LIKE'x'`` is not one).
_IDENT_CHARS = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$')


def _quoted_spans(sql):
    """
    The character spans of ``sql`` that are quoted text or comment.

    A match starting inside one of these is not a statement at all, only text
    that looks like one, so the caller discards it.  One left-to-right pass,
    no backtracking.  An unterminated construct swallows the rest of the
    statement, which is the safe direction: whatever follows is discarded too.
    """
    spans = []
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        following = sql[i + 1:i + 2]
        if char == '-' and following == '-':
            end = sql.find('\n', i + 2)
            end = length if end == -1 else end
            spans.append((i, end))
            i = end
        elif char == '/' and following == '*':
            # Block comments nest in PostgreSQL, unlike in the SQL standard.
            depth = 1
            j = i + 2
            while j < length and depth:
                pair = sql[j:j + 2]
                if pair == '/*':
                    depth += 1
                    j += 2
                elif pair == '*/':
                    depth -= 1
                    j += 2
                else:
                    j += 1
            spans.append((i, min(j, length)))
            i = min(j, length)
        elif char == '$':
            match = _DOLLAR_TAG_RE.match(sql, i)
            if match is None:
                i += 1
                continue
            tag = match.group()
            end = sql.find(tag, match.end())
            end = length if end == -1 else end + len(tag)
            spans.append((i, end))
            i = end
        elif (char in '\'"'
                or (char in 'Ee' and following == "'"
                    and (i == 0 or sql[i - 1] not in _IDENT_CHARS))):
            start = i
            backslash_escapes = char in 'Ee'
            if backslash_escapes:
                i += 1
            quote = sql[i]
            i += 1
            while i < length:
                current = sql[i]
                if backslash_escapes and current == '\\':
                    i += 2
                elif current != quote:
                    i += 1
                elif sql[i + 1:i + 2] == quote:
                    # A doubled quote is one embedded quote, not the end.
                    i += 2
                else:
                    i += 1
                    break
            i = min(i, length)
            spans.append((start, i))
        else:
            i += 1
    return spans


def _is_quoted(spans, pos):
    return any(start <= pos < end for start, end in spans)


def _param_index(sql, pos):
    """Index into ``params`` of the ``%s`` placeholder starting at ``pos``."""
    return sql.count('%s', 0, pos)


def _resolve(token, sql, pos, params):
    """Turn a matched SQL token into a tenant value, ``None`` or ``UNKNOWN``."""
    if token == '%s':
        if params is None:
            return UNKNOWN
        try:
            value = params[_param_index(sql, pos)]
        except (IndexError, KeyError, TypeError):
            return UNKNOWN
        return None if value is None else str(value)
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    if token.upper() in ('DEFAULT', 'NULL'):
        return None
    if _UNQUOTED_NUMBER_RE.match(token):
        return token
    return UNKNOWN


def parse_tenant_statement(sql, params=None):
    """
    Inspect one statement for a change to the configured tenant setting.

    Returns ``NOT_A_SET`` if the statement leaves the tenant alone, ``None`` if
    it clears it, a ``str`` if it sets it, and ``UNKNOWN`` if it touches it in
    a form we decline to interpret.
    """
    return _parse_tenant_statement(sql, params)[0]


def _parse_tenant_statement(sql, params=None):
    """
    As ``parse_tenant_statement``, but also reporting *how* the setting was
    touched, which decides how long the change outlives the statement:

    ``'none'``
        Nothing touched the setting; the value is ``NOT_A_SET``.
    ``'local'``
        ``SET LOCAL`` or ``set_config(..., true)``: the value dies with the
        transaction, and we can follow it.
    ``'reset'``
        ``RESET``, or a session-scoped ``SET ... = DEFAULT``: session-scoped,
        but its end state is exactly the ``None`` we model as "no tenant".
    ``'session'``
        Anything else that touches it.  The value outlives the statement
        somewhere we cannot follow, so it is ``UNKNOWN``.
    """
    guc = cachalot_settings.CACHALOT_TENANT_SETTING
    if guc is None:
        return NOT_A_SET, 'none'
    lowered = sql.lower()
    if guc.lower() not in lowered:
        # The GUC name must appear literally, except when it arrives as a
        # set_config() parameter.  Scanning the parameters is worth it only
        # for that form, and `params` can be very long.
        if not ('set_config' in lowered and params
                and any(p == guc for p in _iter_params(params))):
            return NOT_A_SET, 'none'

    set_re, reset_re, set_config_re = _compile(guc)

    # Collect every construct that touches the configured GUC, discarding the
    # ones that only look like one because they sit in a literal or comment.
    spans = _quoted_spans(sql)
    matches = []

    for match in set_config_re.finditer(sql):
        if _is_quoted(spans, match.start()):
            continue
        name = _resolve(match.group('name'), sql, match.start('name'), params)
        if name is UNKNOWN or name == guc:
            matches.append(('set_config', match, name))

    for match in set_re.finditer(sql):
        if not _is_quoted(spans, match.start()):
            matches.append(('set', match, None))

    for match in reset_re.finditer(sql):
        if not _is_quoted(spans, match.start()):
            matches.append(('reset', match, None))

    if not matches:
        return NOT_A_SET, 'none'
    if '%(' in sql:
        # pyformat placeholders: we cannot map positions to parameters.
        return UNKNOWN, 'session'
    if len(matches) > 1:
        # We cannot tell which construct wins, so we trust none of them.
        return UNKNOWN, 'session'

    match_type, match, name = matches[0]

    if match_type == 'set_config':
        if name is UNKNOWN:
            return UNKNOWN, 'session'
        if match.group('local').lower() not in ('true', 't', "'t'", "'true'"):
            return UNKNOWN, 'session'
        return (_resolve(match.group('value'), sql,
                         match.start('value'), params), 'local')
    elif match_type == 'set':
        if (match.group('scope') or '').upper() != 'LOCAL':
            if match.group('value').upper() == 'DEFAULT':
                # `SET <guc> = DEFAULT` is `RESET <guc>` spelled differently.
                return None, 'reset'
            return UNKNOWN, 'session'
        return (_resolve(match.group('value'), sql,
                         match.start('value'), params), 'local')
    else:  # reset
        return None, 'reset'


def _iter_params(params):
    if isinstance(params, dict):
        return params.values()
    return params
```

- [ ] **Step 4: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS — every test in `cachalot.tests.tenancy`.

- [ ] **Step 5: Commit**

```bash
git add cachalot/tenancy.py cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "feat: Add tenant GUC statement parser"
```

---

### Task 3: Connection tenant state

**Files:**
- Modify: `cachalot/tenancy.py`
- Modify: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: `parse_tenant_statement`, `NOT_A_SET`, `UNKNOWN`, `tenancy_enabled` from Task 2.
- Produces:
  - `observe_statement(connection, sql, params=None, failed=False) -> None` — the single entry point the cursor patch calls.
  - `get_tenant(connection) -> None | str | UNKNOWN`
  - `push_tenant(connection) -> None` / `pop_tenant(connection, committed=True) -> None`
  - `is_partitioned(table) -> bool`
  - `are_all_shared(tables) -> bool`

- [ ] **Step 1: Write the failing state tests**

Append to `cachalot/tests/tenancy.py`:

```python
from django.db import connection, transaction

from ..tenancy import (
    are_all_shared, get_tenant, is_partitioned, observe_statement,
    pop_tenant, push_tenant,
)


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

    def test_disabled_feature_records_nothing(self):
        with override_settings(CACHALOT_TENANT_SETTING=None):
            with transaction.atomic():
                observe_statement(connection,
                                  "SET LOCAL app.tenant_id = '42'")
                self.assertIsNone(get_tenant(connection))


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
```

Update the import in `cachalot/tests/__init__.py`:

```python
from .tenancy import (
    ConnectionTenantTestCase, ParseTenantStatementTestCase,
    TablePredicatesTestCase, TenancySettingsTestCase,
)
```

- [ ] **Step 2: Run to confirm it fails**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: FAIL — `ImportError: cannot import name 'observe_statement'`.

- [ ] **Step 3: Add the state helpers**

Append to `cachalot/tenancy.py`:

```python
def get_tenant(connection):
    """
    The tenant currently in force on ``connection``.

    Always ``None`` outside a transaction: a ``SET LOCAL`` issued in autocommit
    is discarded by PostgreSQL, and this guard also stops a tenant value from
    surviving on a pooled connection past the transaction that set it.
    """
    if not tenancy_enabled() or not connection.in_atomic_block:
        return None
    return getattr(connection, '_cachalot_tenant', None)


def observe_statement(connection, sql, params=None, failed=False):
    """
    Record any tenant change made by a statement that just ran.

    ``failed`` marks a statement that raised: it never took effect in the
    database, so its value must not be trusted.
    """
    if not tenancy_enabled() or not connection.in_atomic_block:
        return
    tenant = parse_tenant_statement(sql, params)
    if tenant is NOT_A_SET:
        return
    connection._cachalot_tenant = UNKNOWN if failed else tenant


def push_tenant(connection):
    """
    Remember the current tenant on entering an atomic block.

    The outermost block also clears the live value.  A matching ``pop_tenant``
    normally does that already, but the clear here means a dropped or skipped
    pop cannot carry one transaction's tenant into the next on a pooled
    connection.  Nested blocks leave it alone so they inherit the outer tenant.
    """
    if not tenancy_enabled():
        return
    stack = getattr(connection, '_cachalot_tenant_stack', None)
    if stack is None:
        stack = connection._cachalot_tenant_stack = []
    stack.append(getattr(connection, '_cachalot_tenant', None))
    if len(stack) == 1:
        connection._cachalot_tenant = None


def pop_tenant(connection, committed=True):
    """
    Undo the tenant bookkeeping of the matching ``push_tenant``.

    The outermost block always lands on ``None``: ending a transaction
    discards every ``SET LOCAL`` made inside it, committed or not.

    A *nested* block follows the database.  PostgreSQL keeps a ``SET LOCAL``
    issued inside a savepoint that is released, so a committed nested block
    leaves its tenant in force in the outer block.  Only a rollback to the
    savepoint undoes it, and only then do we restore what the block inherited.
    """
    stack = getattr(connection, '_cachalot_tenant_stack', None)
    if stack is None:
        # Never pushed on this connection, so there is nothing to restore and
        # nothing to write.  Deliberately not gated on ``tenancy_enabled()``:
        # a block entered while the feature was on must still pop if the
        # setting is toggled off before it exits.
        return
    remembered = stack.pop() if stack else None
    if not stack:
        connection._cachalot_tenant = None
    elif not committed:
        connection._cachalot_tenant = remembered


def is_partitioned(table):
    return (tenancy_enabled()
            and table in cachalot_settings.CACHALOT_PARTITIONED_TABLES)


def are_all_shared(tables):
    if not tenancy_enabled():
        # With the feature off every table is effectively tenant-agnostic,
        # which is what keeps callers that forget to check behave like master.
        return True
    shared = cachalot_settings.CACHALOT_TENANT_SHARED_TABLES
    # A table listed as both partitioned and shared is a contradiction, and
    # the two halves would disagree: the query key would not carry the tenant
    # while the invalidation keys still would, so one tenant's rows could be
    # served to another. Partitioned wins, which is the safe side.
    return bool(tables) and all(table in shared and not is_partitioned(table)
                                for table in tables)
```

- [ ] **Step 4: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS — every test in `cachalot.tests.tenancy`.

- [ ] **Step 5: Commit**

```bash
git add cachalot/tenancy.py cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "feat: Track the current tenant on the database connection"
```

---

### Task 4: Partitioned table cache keys

**Files:**
- Modify: `cachalot/utils.py`
- Modify: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: `is_partitioned` from Task 3; `cachalot_settings.CACHALOT_TABLE_KEYGEN`.
- Produces:
  - `get_read_table_cache_keys(db_alias, table, tenant) -> list[str]`
  - `get_write_table_cache_keys(db_alias, table, tenant) -> list[str]`
  - `get_tenant_query_cache_key(cache_key, tenant) -> str`
  - `GLOBAL_TABLE_SUFFIX`, `TENANT_TABLE_SUFFIX` constants.

The mapping, restated from the spec so you do not have to switch documents:

| call | partitioned table | non-partitioned table |
|---|---|---|
| `read(..., tenant=None)` | `[K_any]` | `[K_any]` |
| `read(..., tenant='42')` | `[K_glob, K_ten(42)]` | `[K_any]` |
| `write(..., tenant=None)` | `[K_any, K_glob]` | `[K_any]` |
| `write(..., tenant='42')` | `[K_any, K_ten(42)]` | `[K_any]` |

- [ ] **Step 1: Write the failing key tests**

Append to `cachalot/tests/tenancy.py`:

```python
from django.db import DEFAULT_DB_ALIAS

from ..utils import (
    get_read_table_cache_keys, get_table_cache_key,
    get_tenant_query_cache_key, get_write_table_cache_keys,
)

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

    def test_tenant_query_cache_key(self):
        base = 'a' * 40
        self.assertNotEqual(get_tenant_query_cache_key(base, '42'), base)
        self.assertNotEqual(get_tenant_query_cache_key(base, '42'),
                            get_tenant_query_cache_key(base, '43'))
        self.assertEqual(get_tenant_query_cache_key(base, '42'),
                         get_tenant_query_cache_key(base, '42'))
```

Add `TableCacheKeysTestCase` to the `from .tenancy import (...)` list in `cachalot/tests/__init__.py`.

- [ ] **Step 2: Run to confirm it fails**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: FAIL — `ImportError: cannot import name 'get_read_table_cache_keys'`.

- [ ] **Step 3: Add the key generators**

In `cachalot/utils.py`, add to the imports near the top:

```python
from .tenancy import is_partitioned
```

(`tenancy` imports only from `settings`, so this introduces no import cycle.)

Then, immediately after the existing `get_table_cache_key()` function:

```python
# Appended to a table name to derive its partitioned keys. The suffixes must
# not collide with a real table name; no Django table contains these.
GLOBAL_TABLE_SUFFIX = ':__cachalot_global__'
TENANT_TABLE_SUFFIX = ':__cachalot_tenant__:'


def get_read_table_cache_keys(db_alias, table, tenant):
    """
    Invalidation keys a read of ``table`` must check under ``tenant``.

    An unscoped read checks the any-write key alone; a scoped read of a
    partitioned table checks the global key and its own tenant key.

    ``UNKNOWN`` is normalised to ``None`` here rather than at each call site,
    so no entry point can derive a key from the sentinel's repr.
    """
    get_table_cache_key = cachalot_settings.CACHALOT_TABLE_KEYGEN
    if tenant is UNKNOWN:
        tenant = None
    if tenant is None or not is_partitioned(table):
        return [get_table_cache_key(db_alias, table)]
    tenant = str(tenant)
    return [get_table_cache_key(db_alias, table + GLOBAL_TABLE_SUFFIX),
            get_table_cache_key(db_alias,
                                table + TENANT_TABLE_SUFFIX + tenant)]


def get_write_table_cache_keys(db_alias, table, tenant):
    """
    Invalidation keys a write to ``table`` must bump under ``tenant``.

    Always the any-write key, which is byte-identical to the key cachalot used
    before partitioning existed; plus, for a partitioned table, either the
    global key (unscoped write) or the tenant's own key.

    ``UNKNOWN`` is normalised to ``None`` here rather than at each call site,
    so no entry point can derive a key from the sentinel's repr.
    """
    get_table_cache_key = cachalot_settings.CACHALOT_TABLE_KEYGEN
    if tenant is UNKNOWN:
        tenant = None
    keys = [get_table_cache_key(db_alias, table)]
    if is_partitioned(table):
        keys.append(get_table_cache_key(
            db_alias,
            table + GLOBAL_TABLE_SUFFIX if tenant is None
            else table + TENANT_TABLE_SUFFIX + str(tenant)))
    return keys


def get_tenant_query_cache_key(cache_key, tenant):
    """Fold a tenant into a query cache key so tenants cannot collide."""
    return sha1(('%s:%s' % (cache_key, tenant)).encode('utf-8')).hexdigest()
```

- [ ] **Step 4: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS — every test in `cachalot.tests.tenancy`.

- [ ] **Step 5: Commit**

```bash
git add cachalot/utils.py cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "feat: Add partitioned table cache key generation"
```

---

### Task 5: Wire tenant tracking into the cursor and atomic patches

After this task the tenant is observable from real SQL, without any behaviour change to caching yet.

**Files:**
- Modify: `cachalot/monkey_patch.py:130-176`
- Modify: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: `observe_statement`, `push_tenant`, `pop_tenant`, `tenancy_enabled` from Task 3.
- Produces: no new callables. `CursorWrapper.execute` now feeds `observe_statement`, and `Atomic.__enter__`/`__exit__` now push/pop the tenant.

- [ ] **Step 1: Write the failing plumbing tests**

Append to `cachalot/tests/tenancy.py`:

```python
@override_settings(CACHALOT_TENANT_SETTING='app.tenant_id')
class TenantPlumbingTestCase(TransactionTestCase):
    def tearDown(self):
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

    def test_real_committed_nested_atomic_keeps_the_inner_tenant(self):
        with transaction.atomic():
            observe_statement(connection, "SET LOCAL app.tenant_id = '42'")
            with transaction.atomic():
                observe_statement(connection, "SET LOCAL app.tenant_id = '43'")
                self.assertEqual(get_tenant(connection), '43')
            # Releasing the savepoint does not undo the inner SET LOCAL.
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
```

Add these imports at the top of the test module if not already present:

```python
from unittest import skipUnless
```

Add `TenantPlumbingTestCase` to the `from .tenancy import (...)` list in `cachalot/tests/__init__.py`.

- [ ] **Step 2: Run to confirm it fails**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy.TenantPlumbingTestCase --noinput -v2`
Expected: FAIL — `test_real_nested_atomic_restores_outer_tenant` reports `'43' != '42'`, because nothing pops the tenant yet.

- [ ] **Step 3: Wire the patches**

In `cachalot/monkey_patch.py`, add to the imports:

```python
from .tenancy import observe_statement, pop_tenant, push_tenant, tenancy_enabled
```

Task 6 extends this import; Task 7 extends it again.

Replace `_patch_cursor()` entirely:

```python
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
                # ``sql`` is not always a str: psycopg3 accepts Composable
                # objects, which have no ``.lower()``.  Skipping them keeps an
                # AttributeError in this ``finally`` from masking the real
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
                            invalidate(
                                *tables, db_alias=connection.alias,
                                cache_alias=cachalot_settings.CACHALOT_CACHE)

        return inner

    if cachalot_settings.CACHALOT_INVALIDATE_RAW or tenancy_enabled():
        CursorWrapper.execute = _patch_cursor_execute(CursorWrapper.execute)
        CursorWrapper.executemany = _patch_cursor_execute(
            CursorWrapper.executemany, is_many=True)
```

Note this moves the `sql.lower()` into a local named `lowered`, because `observe_statement` needs the original casing for quoted literal values.

This raw-SQL `invalidate()` call still invalidates globally. Task 6 adds the `tenant=` argument to it, once `invalidate()` accepts one.

In `_patch_atomic()`, change the two inner functions:

```python
    def patch_enter(original):
        @wraps(original)
        def inner(self):
            cachalot_caches.enter_atomic(self.using)
            original(self)
            # After ``original``: if entering the block raises, ``__exit__``
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
```

- [ ] **Step 4: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS — every test in `cachalot.tests.tenancy`, with the PostgreSQL-only cases skipped.

- [ ] **Step 5: Run the full suite for regressions**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests --noinput -v1`
Expected: `OK`, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add cachalot/monkey_patch.py cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "feat: Observe tenant SET statements and scope them to transactions"
```

---

### Task 6: The invalidation path

Writes start bumping partitioned keys. Reads still use a single key, so the visible behaviour is unchanged — every write still invalidates every tenant. That stays true until Task 7.

**Files:**
- Modify: `cachalot/utils.py` (`_invalidate_tables`)
- Modify: `cachalot/transaction.py` (`AtomicCache.commit`)
- Modify: `cachalot/cache.py` (`CacheHandler.exit_atomic`)
- Modify: `cachalot/api.py` (`invalidate`, `get_last_invalidation`)
- Modify: `cachalot/monkey_patch.py` (`_patch_write_compiler`, the raw-SQL `invalidate` call)
- Modify: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: `get_write_table_cache_keys`, `get_read_table_cache_keys` from Task 4; `get_tenant`, `UNKNOWN` from Task 3.
- Produces:
  - `_invalidate_tables(cache, db_alias, tables, tenant=None)`
  - `invalidate(*tables_or_models, cache_alias=None, db_alias=None, tenant=None)`
  - `get_last_invalidation(*tables_or_models, cache_alias=None, db_alias=None, tenant=None)`
  - `AtomicCache.to_be_invalidated` now holds `(table, tenant)` tuples.
  - `post_invalidation` now carries a `tenant` kwarg.

- [ ] **Step 1: Write the failing invalidation tests**

Append to `cachalot/tests/tenancy.py`:

```python
from contextlib import contextmanager

from ..api import get_last_invalidation, invalidate
from ..signals import post_invalidation
from .models import Test
from .test_utils import FilteredTransactionTestCase, TestUtilsMixin

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
```

Add `TenantInvalidationTestCase` to the `from .tenancy import (...)` list in `cachalot/tests/__init__.py`.

- [ ] **Step 2: Run to confirm it fails**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy.TenantInvalidationTestCase --noinput -v2`
Expected: FAIL — `TypeError: get_last_invalidation() got an unexpected keyword argument 'tenant'`.

- [ ] **Step 3: Take a tenant in `_invalidate_tables`**

In `cachalot/utils.py`, replace `_invalidate_tables` with:

```python
def _invalidate_tables(cache, db_alias, tables, tenant=None):
    tables = filter_cachable(set(tables))
    if not tables:
        return
    if tenant is UNKNOWN:
        # Fail closed: an unresolvable tenant invalidates globally rather
        # than minting a `<cachalot UNKNOWN>` pseudo-tenant key nothing ever
        # reads.  Normalised here so the public `invalidate(..., tenant=...)`
        # is covered too, not just cachalot's own call sites.
        tenant = None
    now = time()
    cache.set_many(
        {key: now
         for table in tables
         for key in get_write_table_cache_keys(db_alias, table, tenant)},
        cachalot_settings.CACHALOT_TIMEOUT)

    if isinstance(cache, AtomicCache):
        # A non-partitioned table ignores the tenant when its keys are built,
        # so buffering one would emit a redundant `post_invalidation` signal
        # per tenant for a table master signals once.
        cache.to_be_invalidated.update(
            (table, tenant if is_partitioned(table) else None)
            for table in tables)
```

- [ ] **Step 4: Group by tenant when committing an atomic cache**

In `cachalot/transaction.py`, replace `AtomicCache.commit` with:

```python
    def commit(self):
        # We import this here to avoid a circular import issue.
        from .utils import _invalidate_tables

        if self:
            self.parent_cache.set_many(
                self, cachalot_settings.CACHALOT_TIMEOUT)
        # The previous `set_many` is not enough.  The parent cache needs to be
        # invalidated in case another transaction occurred in the meantime.
        by_tenant = {}
        for table, tenant in self.to_be_invalidated:
            by_tenant.setdefault(tenant, set()).add(table)
        for tenant, tables in by_tenant.items():
            _invalidate_tables(self.parent_cache, self.db_alias, tables, tenant)
```

- [ ] **Step 5: Send the tenant with the signal**

In `cachalot/cache.py`, in `exit_atomic`, replace the signal loop:

```python
            # This happens when committing the outermost atomic block.
            if not self.atomic_caches[db_alias]:
                for table, tenant in to_be_invalidated:
                    post_invalidation.send(table, db_alias=db_alias,
                                           tenant=tenant)
```

- [ ] **Step 6: Add the `tenant` kwarg to the public API**

In `cachalot/api.py`, change `invalidate`'s signature and body. The signature becomes:

```python
def invalidate(
    *tables_or_models: Tuple[Union[str, Any], ...],
    cache_alias: Optional[str] = None,
    db_alias: Optional[str] = None,
    tenant: Optional[str] = None,
) -> None:
```

Add to its docstring, after the `db_alias` paragraph:

```
    If ``tenant`` is specified, only queries belonging to that tenant are
    invalidated.  The default, ``None``, invalidates every tenant, which is
    what this function has always done.
```

In the body, pass the tenant through and send it with the signal:

```python
        _invalidate_tables(cache, db_alias, tables, tenant)
        invalidated.update(tables)

    if send_signal:
        for table in invalidated:
            post_invalidation.send(table, db_alias=db_alias, tenant=tenant)
```

And add the `:arg tenant:` line to the docstring's argument list.

Change `get_last_invalidation` the same way — signature gains `tenant: Optional[str] = None`, and its key computation becomes:

```python
        table_cache_keys = [key for t in tables
                            for key in get_read_table_cache_keys(db_alias, t,
                                                                 tenant)]
```

Replace the now-unused `get_table_cache_key = cachalot_settings.CACHALOT_TABLE_KEYGEN` line above it, and update the import at the top of `api.py`:

```python
from .utils import _invalidate_tables, get_read_table_cache_keys
```

- [ ] **Step 7: Pass the ambient tenant from the write compiler**

In `cachalot/monkey_patch.py`, replace `_patch_write_compiler`:

```python
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
```

Extend the `cachalot.tenancy` import at the top of the module so `get_tenant` and `UNKNOWN` are available:

```python
from .tenancy import (
    UNKNOWN, get_tenant, observe_statement, pop_tenant, push_tenant,
    tenancy_enabled,
)
```

Then give the raw-SQL path the same treatment, inside `_patch_cursor`'s `inner`:

```python
                        if tables:
                            tenant = get_tenant(connection)
                            invalidate(
                                *tables, db_alias=connection.alias,
                                cache_alias=cachalot_settings.CACHALOT_CACHE,
                                tenant=None if tenant is UNKNOWN else tenant)
```

- [ ] **Step 8: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS — every test in `cachalot.tests.tenancy`, with the PostgreSQL-only cases skipped.

- [ ] **Step 9: Run the full suite for regressions**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests --noinput -v1`
Expected: `OK`, 0 failures. `SignalsTestCase` must still pass — its receivers take `**kwargs`, so the new `tenant` kwarg is harmless.

- [ ] **Step 10: Commit**

```bash
git add cachalot/utils.py cachalot/transaction.py cachalot/cache.py cachalot/api.py cachalot/monkey_patch.py cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "feat: Invalidate partitioned tables per tenant"
```

---

### Task 7: The read path

This closes the loop. After this task the correctness matrix from the spec holds end to end.

**Files:**
- Modify: `cachalot/utils.py` (`_get_table_cache_keys`)
- Modify: `cachalot/monkey_patch.py` (`_patch_compiler`)
- Modify: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: `get_read_table_cache_keys`, `get_tenant_query_cache_key` from Task 4; `get_tenant`, `are_all_shared`, `UNKNOWN` from Task 3.
- Produces: `_get_table_cache_keys(compiler, tenant=None) -> (set[str], list[str])` — **the return type changes from a list to a 2-tuple.** `cachalot/monkey_patch.py` is its only caller in this repo.

- [ ] **Step 1: Write the failing read tests**

Append to `cachalot/tests/tenancy.py`:

```python
from django.contrib.auth.models import User

from .models import TestParent


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
```

Add `PartitionedReadTestCase` and `SharedTableTestCase` to the `from .tenancy import (...)` list in `cachalot/tests/__init__.py`.

- [ ] **Step 2: Run to confirm it fails**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy.PartitionedReadTestCase --noinput -v2`
Expected: FAIL — `test_tenants_do_not_share_cached_results` reports 0 queries where 1 was expected, because both tenants currently share one query cache key.

- [ ] **Step 3: Return the tables alongside the keys**

In `cachalot/utils.py`, replace `_get_table_cache_keys`:

```python
def _get_table_cache_keys(compiler, tenant=None):
    """Returns the tables a query reads and the keys that invalidate it."""
    db_alias = compiler.using
    tables = _get_tables(db_alias, compiler.query, compiler)
    return tables, [key for table in tables
                    for key in get_read_table_cache_keys(db_alias, table,
                                                         tenant)]
```

- [ ] **Step 4: Fold the tenant into the query cache key**

In `cachalot/monkey_patch.py`, extend the imports:

```python
from .tenancy import (
    UNKNOWN, are_all_shared, get_tenant, observe_statement, pop_tenant,
    push_tenant, tenancy_enabled,
)
from .utils import (
    _get_table_cache_keys, _get_tables_from_sql, get_tenant_query_cache_key,
    UncachableQuery, is_cachable, filter_cachable,
)
```

Then replace the body of `_patch_compiler`'s `inner`, from the `db_alias = compiler.using` line to the `return _get_result_or_execute_query(...)` call:

```python
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
```

- [ ] **Step 5: Run the tests**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS — every test in `cachalot.tests.tenancy`, with the PostgreSQL-only cases skipped.

- [ ] **Step 6: Run the full suite for regressions**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests --noinput -v1`
Expected: `OK`, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add cachalot/utils.py cachalot/monkey_patch.py cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "feat: Partition query cache keys and read invalidation by tenant"
```

---

### Task 8: End-to-end verification against PostgreSQL

Everything so far runs on SQLite with the tenant injected through `observe_statement`. This task proves the real path: a genuine `SET LOCAL` on a genuine RLS-protected table.

**Files:**
- Modify: `cachalot/tests/tenancy.py`
- Modify: `cachalot/tests/__init__.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: no new callables.

- [ ] **Step 1: Write the PostgreSQL end-to-end test**

Append to `cachalot/tests/tenancy.py`:

```python
@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL only')
@override_settings(**TENANCY)
class PostgresTenancyTestCase(TestUtilsMixin, FilteredTransactionTestCase):
    """
    Drives the feature the way a real deployment does: the tenant arrives
    only as a PostgreSQL session variable, and an RLS policy — not the ORM —
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
```

Add `PostgresTenancyTestCase` to the `from .tenancy import (...)` list in `cachalot/tests/__init__.py`.

- [ ] **Step 2: Start PostgreSQL and run the suite against it**

The repo's `settings` module expects PostgreSQL on `127.0.0.1:5432` with database `cachalot`, user `cachalot`, password `password` (or `$POSTGRES_PASSWORD`).

Run: `source .venv/bin/activate && DB_ENGINE=postgresql CACHE_BACKEND=locmem python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS, all tests, none skipped.

If PostgreSQL is not available locally, say so in the task report rather than marking this step done — do not delete the test.

- [ ] **Step 3: Confirm SQLite still skips cleanly**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests.tenancy --noinput -v2`
Expected: PASS, with the PostgreSQL cases reported as skipped.

- [ ] **Step 4: Commit**

```bash
git add cachalot/tests/tenancy.py cachalot/tests/__init__.py
git commit -m "test: Verify tenant partitioning against real PostgreSQL RLS"
```

---

### Task 9: Documentation

**Files:**
- Create: `docs/tenancy.rst`
- Modify: `docs/index.rst`
- Modify: `docs/limits.rst`
- Modify: `CHANGELOG.rst`

**Interfaces:**
- Consumes: the settings from Task 1 and the API from Task 6.
- Produces: no code.

- [ ] **Step 1: Write the feature documentation**

Create `docs/tenancy.rst`:

```rst
.. _Tenancy:

Multi-tenancy
-------------

By default django-cachalot keeps one invalidation key per table, so any write
to a table invalidates every cached query on it. In a multi-tenant deployment
that means one tenant's writes constantly throw away every other tenant's
cached reads.

If your tenants are isolated by PostgreSQL Row-Level Security, cachalot can
partition both its cache keys and its invalidation by tenant.

.. warning::

   Under Row-Level Security the SQL text of a query is identical for every
   tenant — the tenant lives in a session variable read by the policy, not in
   the query. Without the settings below, cachalot hashes only the SQL, so two
   tenants collide on one cache key and one tenant can be served the other's
   rows. **If you use RLS, enabling this feature is a correctness requirement,
   not an optimisation.**

Setup
.....

Tell cachalot which session variable carries the tenant::

    CACHALOT_TENANT_SETTING = 'app.tenant_id'

Cachalot then watches statements passing through the database cursor for
``SET LOCAL app.tenant_id = …`` and
``SELECT set_config('app.tenant_id', …, true)``, and remembers the value for
the rest of the transaction. Your application does not need to tell cachalot
anything it is not already telling PostgreSQL.

Then list the tables whose rows are constrained by a policy::

    CACHALOT_PARTITIONED_TABLES = ('shop_order', 'shop_invoice')
    CACHALOT_PARTITIONED_APPS = ('shop',)

Writes to these tables under a tenant no longer invalidate other tenants.

Finally, list any table you know is the same for everyone, so its cached
queries stay shared instead of being duplicated per tenant::

    CACHALOT_TENANT_SHARED_TABLES = ('shop_currency', 'flags_featureflag')

Requirements
............

Cachalot assumes, and cannot check, that **a query run under a tenant sees and
modifies only that tenant's rows.** If a query under one tenant can read
another tenant's rows — a ``BYPASSRLS`` role, a table listed in
``CACHALOT_PARTITIONED_TABLES`` with no policy on it — you will get stale
cross-tenant reads.

The tenant must be set with ``SET LOCAL`` or ``set_config(…, true)`` inside a
transaction, through Django's cursor. Cachalot ignores a tenant set outside a
transaction, because PostgreSQL discards it too.

When cachalot cannot determine the tenant — a connection-scoped ``SET``, a
statement it cannot parse, a statement that raised — it fails closed: queries
on that connection are not cached at all for the rest of the transaction, and
writes invalidate every tenant.

Invalidating by hand
....................

:ref:`invalidate <API>` takes a ``tenant`` argument::

    from cachalot.api import invalidate

    invalidate('shop_order', tenant='42')  # one tenant
    invalidate('shop_order')               # every tenant, the default

``get_last_invalidation`` takes the same argument, and the
``post_invalidation`` signal now carries a ``tenant`` keyword argument.
```

- [ ] **Step 2: Add it to the documentation tree**

In `docs/index.rst`, add `tenancy` to the toctree between `limits` and `api`:

```rst
   introduction
   quickstart
   limits
   tenancy
   api
```

- [ ] **Step 3: Note the assumption in the limits page**

Append to `docs/limits.rst`:

```rst
Row-Level Security
..................

If you isolate tenants with PostgreSQL Row-Level Security, the same SQL returns
different rows for different tenants, and django-cachalot cannot see the
difference. Configure :ref:`multi-tenancy <Tenancy>` or one tenant will be
served another's cached rows.
```

- [ ] **Step 4: Add the changelog entry**

In `CHANGELOG.rst`, insert immediately below the `==============================` header line:

```rst
Unreleased
----------
- Add per-tenant cache partitioning and invalidation for PostgreSQL
  Row-Level Security deployments (``CACHALOT_TENANT_SETTING``,
  ``CACHALOT_PARTITIONED_TABLES``, ``CACHALOT_TENANT_SHARED_TABLES``)

```

- [ ] **Step 5: Verify the documentation builds**

Run: `source .venv/bin/activate && python -m sphinx -b html docs /tmp/cachalot-docs -q`
Expected: no warnings about `tenancy.rst` or undefined references. If Sphinx is not installed, run `uv pip install -r docs/requirements.txt` first; if it still cannot be installed, check the RST by eye and say so in the task report.

- [ ] **Step 6: Run the full suite one last time**

Run: `DJANGO_SETTINGS_MODULE=test_settings_sqlite python -m django test cachalot.tests --noinput -v1`
Expected: `OK`, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add docs/tenancy.rst docs/index.rst docs/limits.rst CHANGELOG.rst
git commit -m "docs: Document per-tenant cache partitioning"
```
