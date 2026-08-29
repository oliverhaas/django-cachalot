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
    guc = cachalot_settings.CACHALOT_TENANT_SETTING
    if guc is None or guc.lower() not in sql.lower():
        # The GUC name must appear literally, except when it arrives as a
        # set_config() parameter, which the check below covers.
        if not (params and guc is not None
                and any(p == guc for p in _iter_params(params))):
            return NOT_A_SET
    if '%(' in sql:
        # pyformat placeholders: we cannot map positions to parameters.
        return UNKNOWN

    set_re, reset_re, set_config_re = _compile(guc)

    # Collect all matches that touch the configured GUC.
    # Fail closed if more than one construct touches it (ambiguous end state).
    matches = []

    # Check set_config matches
    for match in set_config_re.finditer(sql):
        name = _resolve(match.group('name'), sql, match.start('name'), params)
        if name is UNKNOWN or name == guc:
            matches.append(('set_config', match, name))

    # Check SET matches (all of them, including non-LOCAL)
    for match in set_re.finditer(sql):
        matches.append(('set', match, None))

    # Check RESET matches
    for match in reset_re.finditer(sql):
        matches.append(('reset', match, None))

    # If more than one construct touches the GUC, we cannot determine the
    # final state confidently, so fail closed to UNKNOWN.
    if len(matches) > 1:
        return UNKNOWN
    elif len(matches) == 0:
        return NOT_A_SET

    # Exactly one match: process it.
    match_type, match, name = matches[0]

    if match_type == 'set_config':
        if name is UNKNOWN:
            return UNKNOWN
        if match.group('local').lower() not in ('true', 't', "'t'", "'true'"):
            return UNKNOWN
        return _resolve(match.group('value'), sql, match.start('value'), params)
    elif match_type == 'set':
        # Check locality: non-LOCAL SET is unsafe and fails closed.
        if (match.group('scope') or '').upper() != 'LOCAL':
            return UNKNOWN
        return _resolve(match.group('value'), sql, match.start('value'), params)
    else:  # reset
        return None


def _iter_params(params):
    if isinstance(params, dict):
        return params.values()
    return params


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


def pop_tenant(connection):
    """
    Restore the tenant remembered by the matching ``push_tenant``.

    On the outermost block this restores ``None``, which is what ending the
    transaction does to a ``SET LOCAL``. On a committed *nested* block this is
    deliberately conservative: PostgreSQL would keep a ``SET LOCAL`` issued
    inside a released savepoint, we revert it to the tenant that was in force
    when the nested block was entered.
    """
    stack = getattr(connection, '_cachalot_tenant_stack', None)
    if stack is None:
        # Never pushed on this connection, so there is nothing to restore and
        # nothing to write.  Deliberately not gated on ``tenancy_enabled()``:
        # a block entered while the feature was on must still pop if the
        # setting is toggled off before it exits.
        return
    connection._cachalot_tenant = stack.pop() if stack else None


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
