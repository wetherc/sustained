from __future__ import annotations

from typing import Callable, Optional, Type, Union

from ..model import Model

class JoinClauseBuilder:
    def __init__(self, model_class: Type[Model]) -> None: ...
    def join(
        self,
        table: Union[str, Type[Model]],
        on: Union[str, Callable[[JoinClauseBuilder], None]],
        operator: Optional[str] = None,
        to: Optional[str] = None,
    ) -> None: ...
    def innerJoin(
        self,
        table: Union[str, Type[Model]],
        on: Union[str, Callable[[JoinClauseBuilder], None]],
        operator: Optional[str] = None,
        to: Optional[str] = None,
    ) -> None: ...
    def leftJoin(
        self,
        table: Union[str, Type[Model]],
        on: Union[str, Callable[[JoinClauseBuilder], None]],
        operator: Optional[str] = None,
        to: Optional[str] = None,
    ) -> None: ...
    def rightJoin(
        self,
        table: Union[str, Type[Model]],
        on: Union[str, Callable[[JoinClauseBuilder], None]],
        operator: Optional[str] = None,
        to: Optional[str] = None,
    ) -> None: ...
    def outerJoin(
        self,
        table: Union[str, Type[Model]],
        on: Union[str, Callable[[JoinClauseBuilder], None]],
        operator: Optional[str] = None,
        to: Optional[str] = None,
    ) -> None: ...
    def leftOuterJoin(
        self,
        table: Union[str, Type[Model]],
        on: Union[str, Callable[[JoinClauseBuilder], None]],
        operator: Optional[str] = None,
        to: Optional[str] = None,
    ) -> None: ...
    def rightOuterJoin(
        self,
        table: Union[str, Type[Model]],
        on: Union[str, Callable[[JoinClauseBuilder], None]],
        operator: Optional[str] = None,
        to: Optional[str] = None,
    ) -> None: ...
    def crossJoin(self, table: Union[str, Type[Model]]) -> None: ...
    def joinRelated(self, relation_name: str, join_type: str = "inner") -> None: ...
    def innerJoinRelated(self, relation_name: str) -> None: ...
    def leftJoinRelated(self, relation_name: str) -> None: ...
    def rightJoinRelated(self, relation_name: str) -> None: ...
    def OuterJoinRelated(self, relation_name: str) -> None: ...
    def leftOuterJoinRelated(self, relation_name: str) -> None: ...
    def rightOuterJoinRelated(self, relation_name: str) -> None: ...
    def fullOuterJoinRelated(self, relation_name: str) -> None: ...
