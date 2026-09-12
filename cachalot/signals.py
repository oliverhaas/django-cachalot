from django.dispatch import Signal

# sender: name of table invalidated
# db_alias: name of database that was effected
# tenant: tenant the invalidation was scoped to, or None for every tenant
post_invalidation = Signal()
