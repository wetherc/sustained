"""PostgreSQL: the full lifecycle, rehearsed in place."""

from sustained.dialects import Dialects

from . import lifecycle


class PostgresLifecycle(lifecycle.ServerCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = True
