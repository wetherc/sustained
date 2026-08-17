"""DuckDB: in process, so no container and no advisory lock."""

from sustained.dialects import Dialects

from . import lifecycle


class DuckdbLifecycle(lifecycle.ServerCase):
    NAME = "duckdb"
    DIALECT = Dialects.DUCKDB
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = False
