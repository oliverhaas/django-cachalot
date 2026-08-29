# Partitioned (tenant-scoped) cache invalidation

Date: 2026-08-29
Status: approved, pending implementation plan

## Problem

In a multi-tenant deployment where tenant isolation is enforced by PostgreSQL
Row-Level Security (RLS), django-cachalot has two defects.

**Correctness.** Under RLS the SQL text of a query is identical for every
tenant — the tenant lives in a session GUC read by the policy, not in the
query. `get_query_cache_key()` hashes `(db_alias, sql, params)`, so two tenants
running the same queryset collide on one cache key. Tenant B can be served
tenant A's rows. This is a live data leak, not a theoretical one.

**Efficiency.** A single table cache key per table means any write to a table
invalidates that table's cached queries for every tenant. With N tenants
writing continuously, cached reads on shared tables are effectively never warm.

## Solution in one sentence

Split each participating table's invalidation key into three keys — global,
per-tenant, and any-write — chosen so that reads and writes each touch two
keys and no code path ever has to enumerate tenants; and fold the active
tenant into the query cache key so tenants cannot collide.

## Contract and assumptions

The feature rests on one assumption cachalot cannot verify:

> A query executed while tenant `x` is active observes only tenant `x`'s rows,
> and a write executed while tenant `x` is active modifies only tenant `x`'s
> rows.

This is what RLS provides. If it is violated — a `BYPASSRLS` role, a table
listed as partitioned that has no policy, a query deliberately reading across
tenants inside a tenant context — the result is stale cross-tenant reads.

Second assumption, from the deployment convention: the tenant GUC is set
**transaction-locally**, via `SET LOCAL` or `set_config(..., true)`, through
Django's cursor.

## Non-goals

- Enumerating tenants, anywhere, for any purpose.
- Supporting tenancy schemes other than a Postgres session GUC (schema-per-tenant,
  database-per-tenant — the latter is already served by `db_alias`).
- Connection-scoped (non-`LOCAL`) `SET`. See "Fail-closed" below.
- Verifying that a declared partitioned table actually has an RLS policy.

## Design decisions

| Decision | Choice | Rejected alternatives |
|---|---|---|
| Isolation mechanism | Postgres RLS | explicit `.filter(tenant_id=…)` (would need no query-key change) |
| Tenant source | sniff `SET LOCAL` / `set_config` from the cursor | resolver callable setting; cachalot-owned context manager; querying `current_setting()` per query |
| Partitioned table declaration | explicit `CACHALOT_PARTITIONED_TABLES` + `_APPS` | auto-detect by `tenant_id` column; introspect `pg_policies` at startup |
| Query-key partitioning scope | all queries, with a shared-table opt-out | only queries touching partitioned tables; all queries with no opt-out |

The tenant source decision is the load-bearing one. Sniffing derives cachalot's
notion of the current tenant *from* the Postgres state that RLS actually uses,
so the two cannot drift. A Python-side resolver could disagree with the GUC,
and every such disagreement is a cross-tenant leak.

The last two decisions are deliberately asymmetric, because the two halves of
the feature fail differently:

- Mis-declaring a table as partitioned costs **staleness**. It is opt-in.
- Failing to partition the query key of an RLS table costs a **cross-tenant
  leak**. It is on by default for every query once the feature is enabled, and
  sharing must be asserted per table.

## Settings

```python
CACHALOT_TENANT_SETTING       = None   # e.g. 'app.tenant_id'; None disables the feature entirely
CACHALOT_PARTITIONED_TABLES   = ()
CACHALOT_PARTITIONED_APPS     = ()
CACHALOT_TENANT_SHARED_TABLES = ()
```

`CACHALOT_TENANT_SETTING is None` short-circuits every new code path: no cursor
sniffing, no key changes, cache keys byte-identical to the current release.
Existing installations see no behaviour change and no measurable cost.

`CACHALOT_PARTITIONED_TABLES` uses the existing `convert_tables()` converter
(`cachalot/settings.py`), which gives the `_APPS` variant for free and matches
the `CACHALOT_ONLY_CACHABLE_TABLES` / `CACHALOT_UNCACHABLE_TABLES` precedent.

`CACHALOT_TENANT_SHARED_TABLES` names tables known to be tenant-independent
(feature flags, currency tables, static reference data). A query is given a
tenant-independent cache key only if *every* table it touches is in this set.

## Component: `cachalot/tenancy.py` (new)

One module, one job: know the current tenant of a connection. It owns the GUC
parser, the connection-state helpers, and the partitioned/shared table
predicates. It has no knowledge of cache keys.

Public surface:

- `get_tenant(connection) -> str | None | UNKNOWN`
- `parse_tenant_statement(sql, params) -> NOT_A_SET | (value | None | UNKNOWN)`
- `push_tenant(connection)` / `pop_tenant(connection)`
- `is_partitioned(table)` / `are_all_shared(tables)`

State lives on the connection object (`connection._cachalot_tenant`,
`connection._cachalot_tenant_stack`) rather than a thread-local, because it
mirrors DB session state and must follow the connection, not the thread.

### Statement forms recognised

Against the configured GUC name only:

- `SET LOCAL <guc> = <value>` and `SET LOCAL <guc> TO <value>`
- `RESET <guc>` and `SET LOCAL <guc> TO DEFAULT` — clears to `None`
- `SELECT set_config('<guc>', <value>, true)` — the common Django form, since
  `SET LOCAL` accepts no placeholders
- values as SQL literals or positional `%s` placeholders resolved from `params`
- `set_config(..., NULL, ...)` — clears to `None`

### Fail-closed

Any statement that touches the configured GUC but cannot be parsed with
certainty sets the tenant to `UNKNOWN`, which disables caching entirely on that
connection until the transaction ends. This covers pyformat `%(name)s`
placeholders, computed values, and non-`LOCAL` `SET`.

`UNKNOWN` is fail-closed in both directions, and the write direction is the
one that is easy to get wrong:

- **Reads** on a connection with an `UNKNOWN` tenant are not cached at all —
  neither served from cache nor written to it.
- **Writes** on such a connection invalidate **globally** (`K_any` + `K_glob`,
  exactly as an unscoped write), never per-tenant. Attributing a write to the
  last known tenant would leave every other tenant holding stale rows the write
  may have touched.

Rationale: silently retaining a stale tenant value is a cross-tenant leak;
declining to cache, and over-invalidating on write, are merely slow. The parser
is permitted to be incomplete precisely because its failure mode is safe.

Non-`LOCAL` `SET` is routed here rather than supported. Tracking
connection-scoped state correctly would require patching
`BaseDatabaseWrapper.close()` and reasoning about connection pooling, and the
deployment convention this feature targets is transaction-local.

### Transaction scoping

The existing `Atomic.__enter__` / `__exit__` patches gain a push/pop of the
tenant value, alongside the existing `enter_atomic` / `exit_atomic` calls.

This is exact for rollback (Postgres reverts a `SET LOCAL` on rollback to a
savepoint taken before it) and deliberately conservative on the commit of a
nested block: Postgres would retain a `SET LOCAL` issued inside an inner
savepoint after that savepoint is released, whereas we restore the outer value.
The consequence is falling back to unscoped invalidation — correct, less
efficient. Documented, not fixed.

## Component: key scheme

Three table keys per partitioned table. `K_any` is **byte-identical to the
current** `get_table_cache_key(db_alias, table)`, which is what keeps every
partitioning-unaware code path correct: custom `CACHALOT_TABLE_KEYGEN`
implementations, third-party `invalidate()` callers, and cached data written by
an earlier version all continue to mean the right thing.

| Key | Bumped by | Checked by |
|---|---|---|
| `K_any(T)` — today's key | every write to T | unscoped reads |
| `K_glob(T)` | unscoped writes only | scoped reads, all tenants |
| `K_ten(T, x)` | writes in tenant `x` | scoped reads in tenant `x` |

Writes set two keys: `{K_any, K_ten(x)}` when scoped, `{K_any, K_glob}` when
unscoped. Scoped reads fetch two keys per partitioned table, `{K_glob,
K_ten(x)}`; unscoped reads fetch `{K_any}` alone. Non-partitioned tables keep
exactly one key, bumped and checked as today.

### Correctness matrix

| Write | Read | Key that moves | Cached read survives? |
|---|---|---|---|
| tenant `x` | tenant `x` | `K_ten(x)` | no — invalidated, correct |
| tenant `x` | tenant `y ≠ x` | `K_any`, `K_ten(x)` | yes — the point of the feature |
| tenant `x` | unscoped | `K_any` | no — invalidated, correct |
| unscoped | tenant `x`, any `x` | `K_glob` | no — invalidated, correct |
| unscoped | unscoped | `K_any` | no — invalidated, correct |

No row of this table requires knowing which tenants exist.

The `timestamp >= max(table timestamps)` freshness test and the
missing-key-means-stale behaviour in `_get_result_or_execute_query()` are
unchanged; they operate per key and are indifferent to how many keys a table
contributes.

## Component: query cache key

The tenant is folded in *outside* `CACHALOT_QUERY_KEYGEN`, so custom keygens
keep their documented `(compiler)` signature. In `_patch_compiler`:

1. `cache_key = CACHALOT_QUERY_KEYGEN(compiler)` (unchanged; still populates
   `compiler.__cachalot_generated_sql`)
2. `tables, table_cache_keys = _get_table_cache_keys(compiler, tenant)`
3. if a known tenant is active and `not are_all_shared(tables)`, re-hash
   `cache_key` with the tenant appended

An `UNKNOWN` tenant short-circuits before step 1: the query is executed
uncached.

`_get_table_cache_keys()` changes to return `(tables, keys)` so step 3 has the
table set without recomputing it.

## Component: public API and signals

- `invalidate(*tables_or_models, cache_alias=None, db_alias=None, tenant=None)`
  — `tenant=None` means **global**: bump `K_glob` and `K_any`, invalidating
  every tenant. This preserves today's semantics exactly, including when the
  call happens inside a tenant transaction. Narrowing is explicit.
- `get_last_invalidation(*tables_or_models, ..., tenant=None)` — max over the
  keys that tenant's reads would check.
- `post_invalidation.send(table, db_alias=…, tenant=…)` — Django requires
  receivers to accept `**kwargs`, so the added kwarg is backwards compatible.
- `_invalidate_tables(cache, db_alias, tables, tenant=None)`.
- `AtomicCache.to_be_invalidated` becomes a set of `(table, tenant)` tuples,
  grouped by tenant at commit; `CacheHandler.exit_atomic()` emits one signal
  per `(table, tenant)`.

Write paths and the tenant they pass: ORM write compilers and the raw-SQL
cursor invalidation both use the connection's ambient tenant. `post_migrate`
invalidation passes no tenant, i.e. global.

## Files touched

| File | Change |
|---|---|
| `cachalot/tenancy.py` | new — GUC parser, connection tenant state, table predicates |
| `cachalot/settings.py` | four new settings + converters |
| `cachalot/utils.py` | partitioned key generation; `_get_table_cache_keys` returns `(tables, keys)`; `_invalidate_tables` takes `tenant` |
| `cachalot/monkey_patch.py` | cursor patch also sniffs GUCs (and installs regardless of `CACHALOT_INVALIDATE_RAW` when tenancy is on); atomic patch pushes/pops tenant; compiler patch folds tenant into the query key; write compiler passes ambient tenant |
| `cachalot/transaction.py` | `to_be_invalidated` holds `(table, tenant)` |
| `cachalot/cache.py` | `exit_atomic` signals per `(table, tenant)` |
| `cachalot/api.py` | `tenant` kwarg on `invalidate` and `get_last_invalidation` |
| `cachalot/tests/tenancy.py` | new test module |
| `cachalot/tests/__init__.py` | register it |
| `docs/` | new section; `limits.rst` gains the RLS assumption |

## Testing

The GUC parser is a pure function and is unit-tested on every backend, over
each recognised statement form plus the fail-closed cases.

The invalidation matrix is exercised by setting the connection tenant directly,
so it runs on SQLite via `test_settings_sqlite` without needing Postgres. A
Postgres-only case drives a real `SELECT set_config('app.tenant_id', %s, true)`
inside `atomic()` to prove the cursor sniffing is wired up end to end.

Required cases:

1. Each of the five rows of the correctness matrix.
2. Two tenants issuing identical SQL produce different query cache keys, and
   the second tenant does not receive the first's rows. (The leak test.)
3. A query touching only `CACHALOT_TENANT_SHARED_TABLES` produces one key
   across tenants.
4. A non-partitioned table under a tenant context keeps single-key semantics
   and is invalidated by any write.
5. Nested `atomic()` push/pop; rollback restores the outer tenant.
6. An unparsable `SET` of the GUC disables read caching for the rest of the
   transaction, and makes writes in it invalidate globally.
7. A write under an `UNKNOWN` tenant invalidates a cached read belonging to an
   unrelated tenant.
8. With `CACHALOT_TENANT_SETTING = None`, all generated keys are byte-identical
   to those produced before this change.
9. Existing suite passes unchanged (modulo the known SQLite failures:
   `test_cache`, jinja2/template, multi-db).

Tests use `FilteredTransactionTestCase` and call `qs.all()` to defeat Django's
in-queryset result caching, per existing convention in this repo.

## Risks

| Risk | Mitigation |
|---|---|
| Declared-partitioned table has no RLS policy → stale cross-tenant reads | opt-in per table; documented as the feature's central assumption |
| GUC set outside Django's cursor (connection `OPTIONS`, pgbouncer, raw psycopg) → cachalot never sees it, treats everything as unscoped | degrades to today's behaviour (correct, unpartitioned); documented |
| Parser misses a statement form | fail-closed to `UNKNOWN`, which disables caching rather than guessing |
| Cache entry count multiplies by tenant count | `CACHALOT_TENANT_SHARED_TABLES` for genuinely global tables; sizing note in docs |

## Future work

- **Reduce per-read key count for partitioned tables.** Reads on a partitioned
  table fetch two keys rather than one. This is not an extra round trip —
  `_get_result_or_execute_query()` issues a single
  `get_many(table_cache_keys + [cache_key])`, so the cost is payload size, not
  latency. Candidates: collapsing `K_glob` into the per-tenant key by having
  unscoped writes bump a generation counter that the tenant key derives from;
  or storing the pair in one cache entry.
- Auto-detection of partitioned tables from `pg_policies`, as an opt-in
  management command that emits the settings block rather than a startup query.
- Connection-scoped (non-`LOCAL`) `SET` support, if a deployment needs it.
