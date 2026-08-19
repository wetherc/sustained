"""SQLite: the default dialect, in process, with no advisory lock."""

from sustained.dialects import Dialects

from . import lifecycle, queries, transactions, writes


class SqliteLifecycle(lifecycle.ServerCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = False


class SqliteQueries(queries.QueriesCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT


class SqliteWrites(writes.WritesCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT


class SqliteTransactions(transactions.TransactionsCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT
