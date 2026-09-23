"""Method name matching shared by the query builder and its clause builders."""

import functools
from typing import Dict, Optional


def fold_name(name: str) -> str:
    """Lowers a method name and drops its underscores, so whereIn and WHERE_IN compare equal."""
    return name.replace("_", "").lower()


@functools.lru_cache(maxsize=None)
def _public_names(cls: type) -> Dict[str, str]:
    return {fold_name(n): n for n in dir(cls) if not n.startswith("_")}


def resolve_public_name(cls: type, name: str) -> Optional[str]:
    """Returns the defined spelling of a public name on `cls` that `name` folds to, if any."""
    return _public_names(cls).get(fold_name(name))
