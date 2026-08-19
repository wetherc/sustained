"""SQL Server: the full lifecycle. Rehearsal is off the allowlist, so it
runs on a scratch database."""

from sustained.dialects import Dialects

from . import lifecycle, queries, writes


class MssqlLifecycle(lifecycle.ServerCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True


class MssqlQueries(queries.QueriesCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL


class MssqlWrites(writes.WritesCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL
    HAS_RETURNING = False
    HAS_CTAS = False
