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
   tenant: the tenant lives in a session variable read by the policy, not in
   the query. Without the settings below, cachalot hashes only the SQL, so two
   tenants collide on one cache key and one tenant can be served the other's
   rows. **If you use RLS, enabling this feature is a correctness requirement,
   not an optimisation.**

Setup
.....

Tell cachalot which session variable carries the tenant::

    CACHALOT_TENANT_SETTING = 'app.tenant_id'

Cachalot then watches statements passing through the database cursor for
``SET LOCAL app.tenant_id = ...`` and
``SELECT set_config('app.tenant_id', ..., true)``, and remembers the value for
the rest of the transaction. Your application does not need to tell cachalot
anything it is not already telling PostgreSQL.

Then list the tables whose rows are constrained by a policy::

    CACHALOT_PARTITIONED_TABLES = ('shop_order', 'shop_invoice')
    CACHALOT_PARTITIONED_APPS = ('shop',)

Writes to these tables under a tenant no longer invalidate other tenants.

Finally, list any table you know is the same for everyone, so its cached
queries stay shared instead of being duplicated per tenant::

    CACHALOT_TENANT_SHARED_TABLES = ('shop_currency', 'flags_featureflag')

A table listed in both ``CACHALOT_PARTITIONED_TABLES`` and
``CACHALOT_TENANT_SHARED_TABLES`` is treated as partitioned, not shared. This
is deliberate: the two settings would otherwise disagree about the same
table, leaving an unscoped query key alongside per-tenant invalidation keys,
which leaks one tenant's rows to another.

.. note::

   With tenancy enabled, the query cache keys of a partitioned table, and of
   any table not listed in ``CACHALOT_TENANT_SHARED_TABLES``, multiply by the
   number of active tenants, because each tenant caches its own copy of the
   same query. That is the intended design, not a defect, but it is worth
   sizing your cache for. ``CACHALOT_TENANT_SHARED_TABLES`` is the lever for
   tables where that multiplication is not worth paying for.

Requirements
............

Cachalot assumes, and cannot check, that **a query run under a tenant sees and
modifies only that tenant's rows.** If a query under one tenant can read
another tenant's rows, a table listed in ``CACHALOT_PARTITIONED_TABLES`` with
no policy on it, or a database role that bypasses Row-Level Security, you
will get stale cross-tenant reads.

A role with the ``SUPERUSER`` or ``BYPASSRLS`` attribute ignores every
Row-Level Security policy, silently, so the connection cachalot uses must
have neither. ``FORCE ROW LEVEL SECURITY`` is a separate, per-table setting:
it only closes the unrelated gap where the table's own owner would otherwise
bypass its policies, and it does nothing for a superuser or a ``BYPASSRLS``
role.

The tenant must be set with ``SET LOCAL`` or ``set_config(..., true)`` inside a
transaction, through Django's cursor. Cachalot ignores a tenant set outside a
transaction, because PostgreSQL discards it too.

Cachalot fails closed when it cannot determine the tenant, for example on a
connection-scoped ``SET``, a statement it cannot parse, or a statement that
raised: queries stop being cached and writes invalidate every tenant. That
state lasts only until a later statement in the same transaction sets the
tenant to a value cachalot can resolve, not unconditionally to the end of
the transaction.

Invalidating by hand
....................

:ref:`invalidate <API>` takes a ``tenant`` argument::

    from cachalot.api import invalidate

    invalidate('shop_order', tenant='42')  # one tenant
    invalidate('shop_order')               # every tenant, the default

``get_last_invalidation`` takes the same argument, and the
``post_invalidation`` signal now carries a ``tenant`` keyword argument.
