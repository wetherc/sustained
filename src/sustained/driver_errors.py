"""
Classifies the errors DB-API drivers raise, for the paths that treat
one kind of failure as an answer rather than a fault.
"""

import re

# SQLSTATE for an undefined table: Postgres reports 42P01, ODBC 42S02.
_MISSING_TABLE_STATES = frozenset({"42P01", "42S02"})
# MySQL and MariaDB error 1146, ER_NO_SUCH_TABLE.
_MYSQL_NO_SUCH_TABLE = 1146
_MISSING_TABLE_MESSAGE = re.compile(
    r"no such table"  # SQLite
    r"|relation .* does not exist"  # Postgres
    r"|table .* (?:does not|doesn't) exist"  # MySQL, Presto, Trino
    r"|(?:table|schema) with name .* does not exist"  # DuckDB
    r"|invalid object name"  # MSSQL
    r"|table_not_found",  # Presto, Trino, Athena error names
    re.IGNORECASE,
)


def is_missing_table(error: BaseException) -> bool:
    """
    True when the error says the table a statement names does not exist.
    The check reads the SQLSTATE a driver exposes (psycopg's sqlstate,
    psycopg2's pgcode, asyncpg's sqlstate), the code pymysql and pyodbc
    put first in args, and then the message. A closed connection or a
    refused permission reads False.
    """
    for name in ("sqlstate", "pgcode"):
        if getattr(error, name, None) in _MISSING_TABLE_STATES:
            return True
    if error.args and error.args[0] in (_MYSQL_NO_SUCH_TABLE, *_MISSING_TABLE_STATES):
        return True
    return _MISSING_TABLE_MESSAGE.search(str(error)) is not None
