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

    match = set_config_re.search(sql)
    if match is not None:
        name = _resolve(match.group('name'), sql, match.start('name'), params)
        if name is UNKNOWN:
            return UNKNOWN
        if name != guc:
            return NOT_A_SET
        if match.group('local').lower() not in ('true', 't', "'t'", "'true'"):
            return UNKNOWN
        return _resolve(match.group('value'), sql, match.start('value'), params)

    match = set_re.search(sql)
    if match is not None:
        if (match.group('scope') or '').upper() != 'LOCAL':
            return UNKNOWN
        return _resolve(match.group('value'), sql, match.start('value'), params)

    if reset_re.search(sql) is not None:
        return None

    return NOT_A_SET


def _iter_params(params):
    if isinstance(params, dict):
        return params.values()
    return params
