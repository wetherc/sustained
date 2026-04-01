from typing import Optional

from .base import Compiler


class MssqlCompiler(Compiler):
    def quote_identifier(self, identifier: str) -> str:
        return f"[{identifier}]"

    def compile_top(self, value: int) -> str:
        return f"TOP {value}"

    def compile_limit_offset(self, limit: Optional[int], offset: Optional[int]) -> str:
        parts = []
        if offset is not None:
            parts.append(f"OFFSET {offset} ROWS")
            if limit is not None:
                parts.append(f"FETCH NEXT {limit} ROWS ONLY")
        elif limit is not None:
            # MS SQL requires OFFSET for FETCH, which is not ideal.
            # The user should use .top() for simple limits.
            # We will add OFFSET 0 to make FETCH work.
            parts.append("OFFSET 0 ROWS")
            parts.append(f"FETCH NEXT {limit} ROWS ONLY")

        return " ".join(parts)
