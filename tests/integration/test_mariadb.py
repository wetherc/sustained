"""MariaDB: the same dialect as MySQL against a different server."""

from sustained.dialects import Dialects

from . import lifecycle


class MariadbLifecycle(lifecycle.ServerCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True
