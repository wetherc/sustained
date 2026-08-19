"""PostgreSQL: the full lifecycle, rehearsed in place."""

from sustained.dialects import Dialects

from . import lifecycle, queries, transactions, writes


class PostgresLifecycle(lifecycle.ServerCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = True


class PostgresQueries(queries.QueriesCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES


class PostgresWrites(writes.WritesCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES


class PostgresTransactions(transactions.TransactionsCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES
