"""SQL Server: the full lifecycle. Rehearsal is off the allowlist, so it
runs on a scratch database."""

from sustained.dialects import Dialects

from . import lifecycle


class MssqlLifecycle(lifecycle.ServerCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True
