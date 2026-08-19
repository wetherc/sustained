"""PostgreSQL: the full lifecycle, rehearsed in place."""

from sustained.dialects import Dialects

from . import lifecycle, queries


class PostgresLifecycle(lifecycle.ServerCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = True


class PostgresQueries(queries.QueriesCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES
