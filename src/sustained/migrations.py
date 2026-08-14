"""
Explicit, ordered schema migrations.

A Migration pairs an id with an up step and an optional down step. Steps
are a SQL string, a list of SQL strings, or a callable that receives the
connection. The Migrator applies pending migrations in order, records each
applied id in a tracking table, and reverts through the down steps.

There is no automatic diffing against the database catalog: migrations are
written by hand or generated from a model with create_table_migration().
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Type, Union

from sustained.dialects import Dialects
from sustained.execution import transaction

if TYPE_CHECKING:
    from sustained.model import Model

MigrationStep = Union[str, List[str], Callable[[Any], None]]


class Migration:
    """One schema change with an id, an up step, and an optional down step."""

    def __init__(
        self, id: str, up: MigrationStep, down: Optional[MigrationStep] = None
    ) -> None:
        if not id:
            raise ValueError("A migration needs a non-empty id.")
        self.id = id
        self.up = up
        self.down = down


def create_table_migration(model: Type["Model"]) -> Migration:
    """
    Builds a migration that creates the model's table from its tableColumns
    on the way up and drops it on the way down. The migration id is
    'create_<tableName>'.
    """
    return Migration(
        id=f"create_{model.tableName}",
        up=model.create_table_sql(),
        down=model.drop_table_sql(),
    )


def _run_step(connection: Any, step: MigrationStep) -> None:
    if callable(step):
        step(connection)
        return
    statements = [step] if isinstance(step, str) else list(step)
    cursor = connection.cursor()
    for statement in statements:
        cursor.execute(statement)


class Migrator:
    """
    Applies and reverts an ordered list of migrations on one connection.

    Applied migration ids live in a tracking table, created on first use.
    Each migration runs inside a transaction, so a failing step leaves the
    schema at the previous migration. Engines that do not support
    transactional DDL may still leave partial changes from a multi-step
    migration.
    """

    def __init__(
        self,
        connection: Any,
        migrations: List[Migration],
        table: str = "sustained_migrations",
        dialect: Dialects = Dialects.DEFAULT,
    ) -> None:
        ids = [m.id for m in migrations]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate migration ids: {sorted(duplicates)}.")
        self._connection = connection
        self._migrations = list(migrations)
        self._table = table
        self._dialect = dialect
        self._compiler = Dialects.get_compiler(dialect)

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    def _ensure_tracking_table(self) -> None:
        from sustained.schema import String, Text, build_create_table_sql

        sql = build_create_table_sql(
            self._compiler,
            self._table_sql(),
            {
                "id": String(255, primary_key=True),
                "applied_at": Text(nullable=False),
            },
            if_not_exists=True,
        )
        self._connection.cursor().execute(sql)
        if hasattr(self._connection, "commit"):
            self._connection.commit()

    def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        self._ensure_tracking_table()
        cursor = self._connection.cursor()
        cursor.execute(f"SELECT id FROM {self._table_sql()} ORDER BY applied_at, id")
        return [row[0] for row in cursor.fetchall()]

    def pending(self) -> List[Migration]:
        """Returns the registered migrations that have not been applied."""
        applied = set(self.applied())
        return [m for m in self._migrations if m.id not in applied]

    def status(self) -> List[tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(self.applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    def up(self, target: Optional[str] = None) -> List[str]:
        """
        Applies pending migrations in order, stopping after the target id
        when one is given. Returns the ids that were applied.
        """
        migrations = self._migrations
        if target is not None:
            ids = [m.id for m in migrations]
            if target not in ids:
                raise ValueError(f"Unknown migration target: {target!r}.")
            migrations = migrations[: ids.index(target) + 1]

        already_applied = set(self.applied())
        placeholder = self._compiler.placeholder()
        applied_now: List[str] = []
        for migration in migrations:
            if migration.id in already_applied:
                continue
            with transaction(self._connection):
                _run_step(self._connection, migration.up)
                timestamp = datetime.now(timezone.utc).isoformat()
                self._connection.cursor().execute(
                    f"INSERT INTO {self._table_sql()} (id, applied_at) "
                    f"VALUES ({placeholder}, {placeholder})",
                    (migration.id, timestamp),
                )
            applied_now.append(migration.id)
        return applied_now

    def sync(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
    ) -> List[str]:
        """
        Diffs the database against the models, registers the generated
        migration, and applies everything pending. Returns the applied ids;
        an empty list means the schema was already up to date.

        Additive changes generate reversible steps, so down() rolls the
        sync back. Drops require allow_drops=True and are not reversible.
        """
        from sustained.autogenerate import autogenerate

        self._ensure_tracking_table()
        generated_id = migration_id or datetime.now(timezone.utc).strftime(
            "auto_%Y%m%d%H%M%S_%f"
        )
        migration = autogenerate(
            self._connection,
            models,
            id=generated_id,
            dialect=self._dialect,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            exclude_tables=(self._table,),
        )
        if migration is not None:
            self._migrations.append(migration)
        return self.up()

    def down_to(self, target: str) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        """
        applied = self.applied()
        if target not in applied:
            raise ValueError(f"Migration '{target}' is not applied.")
        steps = len(applied) - applied.index(target) - 1
        return self.down(steps) if steps else []

    def down(self, steps: int = 1) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step and be registered with
        this migrator. Returns the ids that were reverted.
        """
        applied = self.applied()
        by_id = {m.id: m for m in self._migrations}
        placeholder = self._compiler.placeholder()
        reverted: List[str] = []
        for migration_id in reversed(applied[-steps:] if steps else []):
            migration = by_id.get(migration_id)
            if migration is None:
                raise ValueError(
                    f"Applied migration '{migration_id}' is not registered "
                    "with this migrator; cannot revert."
                )
            if migration.down is None:
                raise ValueError(f"Migration '{migration_id}' has no down step.")
            with transaction(self._connection):
                _run_step(self._connection, migration.down)
                self._connection.cursor().execute(
                    f"DELETE FROM {self._table_sql()} WHERE id = {placeholder}",
                    (migration_id,),
                )
            reverted.append(migration_id)
        return reverted
