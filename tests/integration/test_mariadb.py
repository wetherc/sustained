"""MariaDB: the same dialect as MySQL against a different server."""

from sustained.dialects import Dialects

from . import lifecycle, queries, transactions, writes


class MariadbLifecycle(lifecycle.ServerCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True


class MariadbQueries(queries.QueriesCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL


class MariadbWrites(writes.WritesCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL
    HAS_RETURNING = False


class MariadbTransactions(transactions.TransactionsCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL
