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
