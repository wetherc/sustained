"""MySQL: the full lifecycle. Schema changes do not roll back, so the
rehearsal runs on a scratch database."""

from sustained.dialects import Dialects

from . import lifecycle, queries, transactions, writes


class MysqlLifecycle(lifecycle.ServerCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True


class MysqlQueries(queries.QueriesCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL


class MysqlWrites(writes.WritesCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL
    HAS_RETURNING = False


class MysqlTransactions(transactions.TransactionsCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL
