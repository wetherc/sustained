"""DuckDB: in process, so no container and no advisory lock."""

from sustained.dialects import Dialects

from . import lifecycle, queries, transactions, writes


class DuckdbLifecycle(lifecycle.ServerCase):
    NAME = "duckdb"
    DIALECT = Dialects.DUCKDB
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = False


class DuckdbQueries(queries.QueriesCase):
    NAME = "duckdb"
    DIALECT = Dialects.DUCKDB


class DuckdbWrites(writes.WritesCase):
    NAME = "duckdb"
    DIALECT = Dialects.DUCKDB


class DuckdbTransactions(transactions.TransactionsCase):
    NAME = "duckdb"
    DIALECT = Dialects.DUCKDB
