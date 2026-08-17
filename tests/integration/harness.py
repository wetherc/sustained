"""
Opens a connection to a real server for the integration tests.

support.json says which servers exist, which driver each one needs, and
which environment variable holds its connection string. This module reads
that file, opens the connection, and skips the test with an actionable
message when either the variable or the driver is missing.
"""

import importlib
import json
import os
import sqlite3
import unittest
from collections import namedtuple
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = json.loads((ROOT / "support.json").read_text())
ROWS = {row["name"]: row for row in SUPPORT["databases"]}

Parts = namedtuple("Parts", "host port user password database")


def strict():
    """True when a missing server must fail instead of skip."""
    return os.environ.get("SUSTAINED_TEST_STRICT") == "1"


def _stop(name, message):
    if strict():
        raise AssertionError(f"{name}: {message}")
    raise unittest.SkipTest(f"{name}: {message}")


def dsn(name):
    """
    The connection string for a server. Falls back to the default in
    support.json for the servers that run in this process; every other
    server needs its environment variable set.
    """
    row = ROWS[name]
    value = os.environ.get(row["dsn_env"] or "")
    if value:
        return value
    if row["server"] == "in-process":
        return row["dsn"]
    if row["server"] == "account":
        _stop(
            name,
            f"{row['dsn_env']} is not set. Point it at a staging S3 "
            "directory, and set AWS_PROFILE or the AWS key variables.",
        )
    _stop(
        name,
        f"{row['dsn_env']} is not set. Start the server and run the tests "
        f"with: python3 matrix.py {name}",
    )


def driver(name):
    """The driver module for a server, or a skip naming what to install."""
    row = ROWS[name]
    try:
        return importlib.import_module(row["module"])
    except ImportError:
        _stop(name, f"the {row['module']} driver is missing. Install {row['driver']}")


def parts(url):
    """Splits a connection string into the pieces a driver asks for."""
    parsed = urlparse(url)
    return Parts(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=parsed.path.lstrip("/"),
    )


def _connect_postgres(name):
    return driver(name).connect(dsn(name))


def _connect_mysql(name, database=None):
    piece = parts(dsn(name))
    return driver(name).connect(
        host=piece.host,
        port=piece.port or 3306,
        user=piece.user,
        password=piece.password,
        database=database or piece.database,
        autocommit=False,
    )


def _odbc_string(piece, database):
    odbc = os.environ.get(
        "SUSTAINED_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"
    )
    return (
        f"DRIVER={{{odbc}}};SERVER={piece.host},{piece.port or 1433};"
        f"UID={piece.user};PWD={piece.password};DATABASE={database};"
        "TrustServerCertificate=yes"
    )


def _connect_mssql(name, database=None):
    piece = parts(dsn(name))
    pyodbc = driver(name)
    wanted = database or piece.database
    # The image ships with master only, so the test database is made on
    # first use. CREATE DATABASE cannot run inside a transaction.
    bootstrap = pyodbc.connect(_odbc_string(piece, "master"), autocommit=True)
    try:
        bootstrap.cursor().execute(
            f"IF DB_ID('{wanted}') IS NULL CREATE DATABASE [{wanted}]"
        )
    finally:
        bootstrap.close()
    return pyodbc.connect(_odbc_string(piece, wanted))


def _connect_sqlite(name):
    return sqlite3.connect(dsn(name))


def _connect_duckdb(name):
    return driver(name).connect(dsn(name))


def _connect_presto(name):
    piece = parts(dsn(name))
    return driver(name).dbapi.connect(
        host=piece.host,
        port=piece.port or 8080,
        user=piece.user or "sustained",
        catalog=piece.database or "tpch",
        schema="tiny",
    )


def _connect_athena(name):
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    kwargs = {
        "s3_staging_dir": dsn(name),
        "region_name": region,
        "schema_name": os.environ.get("SUSTAINED_TEST_ATHENA_SCHEMA", "default"),
    }
    profile = os.environ.get("AWS_PROFILE")
    if profile:
        kwargs["profile_name"] = profile
    return driver(name).connect(**kwargs)


CONNECTORS = {
    "postgres": _connect_postgres,
    "mysql": _connect_mysql,
    "mariadb": _connect_mysql,
    "mssql": _connect_mssql,
    "sqlite": _connect_sqlite,
    "duckdb": _connect_duckdb,
    "presto": _connect_presto,
    "athena": _connect_athena,
}


def connect(name):
    """A connection to the named server, or a skip explaining what is missing."""
    return CONNECTORS[name](name)


def connect_scratch(name):
    """
    A connection to a throwaway database on the same server, for the
    servers whose schema changes do not roll back. The database is created
    on first use.
    """
    scratch = os.environ.get("SUSTAINED_TEST_SCRATCH_DB", "sustained_scratch")
    if name in ("mysql", "mariadb"):
        admin = _connect_mysql(name)
        try:
            admin.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{scratch}`")
            admin.commit()
        finally:
            admin.close()
        return _connect_mysql(name, database=scratch)
    if name == "mssql":
        return _connect_mssql(name, database=scratch)
    raise ValueError(f"{name} rehearses in place and needs no scratch database")
