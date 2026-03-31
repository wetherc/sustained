from enum import Enum

from .compilers.base import Compiler
from .compilers.presto import PrestoCompiler


class Dialects(Enum):
    PRESTO = PrestoCompiler
    DEFAULT = Compiler

    @staticmethod
    def get_compiler(dialect: "Dialects") -> Compiler:
        compiler_class: type[Compiler] = dialect.value
        return compiler_class(dialect)
