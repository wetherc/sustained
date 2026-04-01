from enum import Enum

from sustained.compilers.base import Compiler
from sustained.compilers.mssql import MssqlCompiler
from sustained.compilers.postgres import PostgresCompiler
from sustained.compilers.presto import PrestoCompiler


class Dialects(Enum):
    PRESTO = PrestoCompiler
    MSSQL = MssqlCompiler
    POSTGRES = PostgresCompiler
    DEFAULT = Compiler

    @staticmethod
    def get_compiler(dialect: "Dialects") -> Compiler:
        compiler_class: type[Compiler] = dialect.value
        return compiler_class(dialect)
