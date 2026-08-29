import re
from bisect import bisect_right
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


# A pyformat placeholder is matched so that a construct spelled with one is
# read as a value we cannot resolve rather than as no construct at all.
_PYFORMAT = r'%\([^)]*\)s'


@lru_cache(maxsize=8)
def _compile(guc):
    name = re.escape(guc)
    return (
        # SET [LOCAL|SESSION] <guc> {=|TO} <value>
        re.compile(
            r'\bSET\s+(?:(?P<scope>LOCAL|SESSION)\s+)?"?%s"?\s*'
            r'(?:=|\bTO\b)\s*(?P<value>\'(?:[^\']|\'\')*\'|%s|[^\s;,)]+)'
            % (name, _PYFORMAT),
            re.IGNORECASE),
        # RESET <guc>
        re.compile(r'\bRESET\s+"?%s"?' % name, re.IGNORECASE),
        # set_config(<name>, <value>, <is_local>)
        re.compile(
            r'\bset_config\s*\(\s*'
            r"(?P<name>'(?:[^']|'')*'|%%s|%(pyformat)s)\s*,\s*"
            r"(?P<value>'(?:[^']|'')*'|%%s|%(pyformat)s|NULL)\s*,\s*"
            r'(?P<local>%(pyformat)s|[^\s,)]+)\s*\)'
            % {'pyformat': _PYFORMAT},
            re.IGNORECASE),
    )


_DOLLAR_TAG_RE = re.compile(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$')

# An `E` only opens a string if it stands alone: `LIKE'x'` ends in one too.
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
        elif char == '$' and (i == 0 or sql[i - 1] not in _IDENT_CHARS):
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


def _is_quoted(spans, starts, pos):
    """Whether ``pos`` falls in one of the non-overlapping, ordered ``spans``."""
    index = bisect_right(starts, pos) - 1
    return index >= 0 and pos < spans[index][1]


def _resolve(token, sql, pos, params):
    """Turn a matched SQL token into a tenant value, ``None`` or ``UNKNOWN``."""
    if token == '%s':
        if params is None:
            return UNKNOWN
        # psycopg counts a `%s` inside a literal as a placeholder too, but
        # `%%s` is an escaped percent sign rather than one.
        index = i = 0
        while i < pos:
            if sql[i] != '%':
                i += 1
            elif sql.startswith('%%', i):
                i += 2
            else:
                index += sql.startswith('%s', i)
                i += 1
        try:
            value = params[index]
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
    As ``parse_tenant_statement``, plus how long the change outlives the
    statement: ``'none'`` (nothing touched the setting), ``'local'``
    (``SET LOCAL`` or ``set_config(..., true)``), ``'reset'`` (back to the
    default, the ``None`` the rest of the module models as no tenant) or
    ``'session'`` (it outlives the statement where we cannot follow it,
    hence ``UNKNOWN``).
    """
    guc = cachalot_settings.CACHALOT_TENANT_SETTING
    if guc is None:
        return NOT_A_SET, 'none'
    lowered = sql.lower()
    if guc.lower() not in lowered:
        # The GUC name must appear literally, except when it arrives as a
        # set_config() parameter.  Scanning the parameters is worth it only
        # for that form, and `params` can be very long.
        values = params.values() if isinstance(params, dict) else params
        if 'set_config' not in lowered or guc not in (values or ()):
            return NOT_A_SET, 'none'

    set_re, reset_re, set_config_re = _compile(guc)

    # Constructs sitting in a literal or comment only look like one.
    spans = _quoted_spans(sql)
    starts = [span[0] for span in spans]
    matches = []

    for match in set_config_re.finditer(sql):
        if _is_quoted(spans, starts, match.start()):
            continue
        name = _resolve(match.group('name'), sql, match.start('name'), params)
        if name is UNKNOWN or name == guc:
            matches.append(('set_config', match, name))

    for match in set_re.finditer(sql):
        if not _is_quoted(spans, starts, match.start()):
            matches.append(('set', match, None))

    for match in reset_re.finditer(sql):
        if not _is_quoted(spans, starts, match.start()):
            matches.append(('reset', match, None))

    if not matches:
        return NOT_A_SET, 'none'
    if len(matches) > 1:
        # We cannot tell which one wins, so we trust none of them.
        return UNKNOWN, 'session'

    match_type, match, name = matches[0]

    if match_type == 'set_config':
        local = match.group('local').lower() in ('true', 't', "'t'", "'true'")
        if not local:
            return UNKNOWN, 'session'
        if name is UNKNOWN:
            return UNKNOWN, 'local'
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


def get_tenant(connection):
    """
    The tenant currently in force on ``connection``.

    A ``SET LOCAL`` only exists inside a transaction, so outside one the
    answer is ``None`` - which also stops a value from surviving on a pooled
    connection past the transaction that set it.  The exception is a
    connection whose setting was touched outside ``SET LOCAL`` semantics:
    that value outlives the statement that made it and we cannot see it, so
    the connection is distrusted until a ``SET LOCAL`` overrides it.
    """
    if not tenancy_enabled():
        return None
    dirty = getattr(connection, '_cachalot_session_tenant_dirty', False)
    if not connection.in_atomic_block:
        return UNKNOWN if dirty else None
    tenant = getattr(connection, '_cachalot_tenant', None)
    if tenant is None and dirty:
        # No ``SET LOCAL`` is masking the session value, so it shows through.
        return UNKNOWN
    return tenant


def observe_statement(connection, sql, params=None, failed=False):
    """
    Record any tenant change made by a statement that just ran.

    ``failed`` marks a statement that raised: it never took effect in the
    database, so its value must not be trusted.
    """
    if not tenancy_enabled():
        return
    tenant, scope = _parse_tenant_statement(sql, params)
    if scope == 'session':
        # Sticky by design: nothing tells us when a session-scoped value
        # changes again, only a RESET tells us where it ended up, and a
        # reconnect is invisible, so the flag lives as long as the wrapper.
        connection._cachalot_session_tenant_dirty = True
    elif (scope == 'reset' and not failed
            and not connection.in_atomic_block):
        # Only out here is a RESET final.  Inside a transaction a rollback
        # would put the session value back without telling us.
        connection._cachalot_session_tenant_dirty = False
    if tenant is NOT_A_SET or not connection.in_atomic_block:
        # A ``SET LOCAL`` outside a transaction is discarded by PostgreSQL.
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
        # Not gated on ``tenancy_enabled()``: a block entered while the
        # feature was on must still pop if it is switched off before exit.
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
