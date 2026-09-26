"""
Rehearsals: what one proved, the key it earned, and the rows it writes.

A rehearsal applies the pending migrations, runs their down steps, and
rolls everything back. A passing one leaves rows in the rehearsal table,
keyed to the applied history it started from and the statements it ran.
A run that would remove data looks for such a row first.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)

from sustained.dialects import Dialects
from sustained.migrations.migration import (
    AppliedRecord,
    Migration,
    _legacy_checksum,
    migration_checksum,
    migration_sql,
)
from sustained.types import SqlValue

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler


class RehearsalResult(NamedTuple):
    """
    What a rehearsal proved about one migration.

    `up_ok` reports whether the up step ran, or None when the rehearsal
    left the migration out: a migration with transactional=False runs
    outside a transaction, and the rehearsal cannot roll such a run back.
    `down_ok` reports whether the down step ran, or None when nothing was
    proved, and `error` then says why: the migration has no down step, the
    sweep never reached it, or it is a repeatable, which has no down step
    to prove. When a step raised, `error` holds the database error.

    `landed` and `reversed` carry what the schema itself said. Each is
    None when it was not checked, an empty list when it was checked and
    proved, and a list of readable lines when it failed. `landed` is only
    checked for the migration generated from the models, since a
    hand-written migration may create objects no model declares.
    `reversed` compares the schema after the down sweep against the
    snapshot taken before the rehearsal, so it is shared by every
    migration in the run: a leftover object names the whole sweep, not
    one step of it. It stays None unless every step in the run reversed,
    since changes a migration without a down step leaves behind cannot be
    charged to the steps that did come back.
    """

    id: str
    up_ok: Optional[bool]
    down_ok: Optional[bool]
    error: Optional[str]
    landed: Optional[List[str]] = None
    reversed: Optional[List[str]] = None


def rehearsal_failed(result: RehearsalResult) -> bool:
    """
    Whether one result stops a rehearsal from passing: a step that raised,
    a down step that failed, models that did not land, or a schema that
    did not come back. A step that could not be proved, which includes a
    migration the rehearsal left out for running outside a transaction, is
    not a failure.
    """
    return (
        result.up_ok is False
        or result.down_ok is False
        or bool(result.landed)
        or bool(result.reversed)
    )


class Rehearsal(List[RehearsalResult]):
    """
    A rehearsal's results, one per migration that ran, plus the row it
    earned.

    The class is a list, so it iterates and indexes like the plain list
    earlier versions returned. `key` names the exact content the rehearsal
    covered: the applied history it started from and the statements it
    ran. `recorded` says whether the row reached the tracking
    database, which a scratch rehearsal leaves to the caller.
    """

    def __init__(
        self,
        results: Iterable[RehearsalResult],
        key: str,
        recorded: bool = False,
    ) -> None:
        super().__init__(results)
        self.key = key
        self.recorded = recorded

    @property
    def ok(self) -> bool:
        """True when every result passed."""
        return not any(rehearsal_failed(r) for r in self)


# The outcomes a rehearsal row can hold. 'override' marks statements that
# were applied with unrehearsed=True: nothing proved them, and the row is
# there so the database says who skipped the proof and when.
REHEARSAL_PASSED = "passed"
REHEARSAL_FAILED = "failed"
REHEARSAL_OVERRIDE = "override"


def _rehearsal_token(checksum: Optional[str], migration_id: str) -> str:
    """
    One entry in a rehearsal key. A callable step has no SQL to hash and no
    explicit checksum, so its id stands in: the token keeps a mixed set
    ordered and hashable, and a callable can never trigger the gate on its
    own, since the destructive scan cannot read it either.
    """
    return checksum if checksum is not None else f"id:{migration_id}"


class Digest(Protocol):
    """The part of a hashlib hash the rehearsal keys use."""

    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...

    def copy(self) -> "Digest": ...


def _history_digest(applied: Sequence[AppliedRecord]) -> Digest:
    """
    A digest holding the applied history alone. A key adds the marker that
    separates the history from the run, so a digest that stops here can
    still take more history first.
    """
    digest = hashlib.sha256()
    digest.update(b"applied\n")
    for record in applied:
        if not record.success:
            continue
        digest.update(_rehearsal_token(record.checksum, record.id).encode("utf-8"))
        digest.update(b"\n")
    return digest


def _applied_digest(applied: Sequence[AppliedRecord]) -> Digest:
    """
    A digest holding the applied history and ready for a run's migrations.
    Prefix keys copy it instead of hashing the history again per prefix.
    """
    digest = _history_digest(applied)
    digest.update(b"run\n")
    return digest


def _digest_migration(digest: Digest, migration: Migration) -> None:
    """Adds one migration's token to a digest built by _applied_digest()."""
    token = _rehearsal_token(migration_checksum(migration), migration.id)
    digest.update(token.encode("utf-8"))
    digest.update(b"\n")


def rehearsal_key(applied: Sequence[AppliedRecord], run: Sequence[Migration]) -> str:
    """
    The SHA-256 hex digest that names one rehearsal: the checksums of the
    successful tracking rows the run starts from, then the checksums of
    the migrations it runs.

    The applied history is part of the key because a rehearsal proves a
    set of statements against one starting schema. A database with a
    different history must not accept the row.

    Ids are not hashed, only statements, so a generated migration that
    takes a new timestamped id between the rehearsal and the run keeps the
    same key.
    """
    digest = _applied_digest(applied)
    for migration in run:
        _digest_migration(digest, migration)
    return digest.hexdigest()


def _legacy_rehearsal_key(
    applied: Sequence[AppliedRecord], run: Sequence[Migration]
) -> Optional[str]:
    """
    The key a release before 2.25.0 wrote for this run, or None when it
    is the same as rehearsal_key(). Those releases hashed each pending
    migration's legacy checksum. The applied history reads the stored
    checksums either way, and a database those releases wrote stores the
    legacy ones until repair() rewrites them.
    """
    digest = _applied_digest(applied)
    for migration in run:
        token = _rehearsal_token(_legacy_checksum(migration), migration.id)
        digest.update(token.encode("utf-8"))
        digest.update(b"\n")
    key = digest.hexdigest()
    return None if key == rehearsal_key(applied, run) else key


def _destructive_in(
    run: Sequence[Migration], compiler: Optional["Compiler"] = None
) -> List[Tuple[str, str]]:
    """
    The (migration id, statement) pairs in a run that remove data. A
    callable step renders no SQL and is invisible here, the same limit the
    plan command's labels carry. Ddl steps render for the given compiler's
    dialect, so the labels read the SQL the run would run.
    """
    from sustained.analysis import destructive_statements

    found: List[Tuple[str, str]] = []
    for migration in run:
        if callable(migration.up):
            continue
        statements = migration_sql(migration, "up", compiler)
        for statement in destructive_statements(statements):
            found.append((migration.id, statement))
    return found


def _destructive_prefix_keys(
    applied: Sequence[AppliedRecord],
    pending: Sequence[Migration],
    compiler: Optional["Compiler"] = None,
) -> List[str]:
    """
    The keys a targeted run would look for.

    up(target=...) applies the versioned migrations up to the target and
    skips the repeatables, so its run set is a slice of the versioned
    pending list. A rehearsal applied every one of those slices on its way
    up and took them all back on the way down, so it proved them all.

    A key names the applied history a run starts from as well as the
    statements it runs, so one targeted run changes the key the next one
    looks for. The keys therefore cover every start point too: the
    history the rehearsal began with, then that history with the first
    migration applied, and so on. Without them, up(target=A) followed by
    up(target=B) asked for a key nothing had recorded, and the second run
    demanded a rehearsal it had already passed.

    An up() without a target that follows targeted runs applies the rest
    of the versioned list and then the pending repeatables, which is the
    tail of the rehearsal's own run. Each start point therefore also gets
    a key for that tail, or up(target=A) followed by up() asked for a key
    nothing had recorded.

    Only slices that remove data get a key: nothing else ever reads the
    rehearsal table, and a row per slice on every rehearsal would be
    waste. Each migration's statements render once, and the digests are
    copied rather than rebuilt, so the cost is one render per migration
    and one hash per key.
    """
    versioned = [m for m in pending if not m.repeatable]
    repeatables = [m for m in pending if m.repeatable]
    removes = [bool(_destructive_in([m], compiler)) for m in versioned]
    repeatables_remove = bool(_destructive_in(repeatables, compiler))

    # Checksums come out once per migration; the slice loops below would
    # otherwise recompute each one per (start, end) pair.
    def encoded(migration: Migration) -> bytes:
        checksum = migration_checksum(migration)
        return (_rehearsal_token(checksum, migration.id) + "\n").encode("utf-8")

    tokens = [encoded(m) for m in versioned]
    tail = [encoded(m) for m in repeatables]
    history = _history_digest(applied)
    keys: List[str] = []
    seen: Set[str] = set()

    def add(digest: Digest) -> None:
        key = digest.hexdigest()
        if key not in seen:
            seen.add(key)
            keys.append(key)

    # The last start point has every versioned migration applied, so only
    # the repeatables remain for it.
    for start in range(len(versioned) + 1):
        digest = history.copy()
        digest.update(b"run\n")
        # A slice removes data as soon as one of its migrations does, so
        # the flag never goes back.
        destructive = False
        for index in range(start, len(versioned)):
            digest.update(tokens[index])
            destructive = destructive or removes[index]
            if destructive:
                add(digest)
        if tail and (destructive or repeatables_remove):
            for token in tail:
                digest.update(token)
            add(digest)
        # The next start point begins where this one's first migration
        # has already applied.
        if start < len(versioned):
            history.update(tokens[start])
    return keys


def _passed_rehearsal_keys(
    applied: Sequence[AppliedRecord],
    pending: Sequence[Migration],
    key: str,
    has_drift: bool,
    compiler: Optional["Compiler"] = None,
) -> List[str]:
    """
    Every key a passing rehearsal on the real database proves: the full
    run's key, then the registered migrations' key when a model diff ran
    too (a run without models applies those and stops), then the
    destructive prefix keys. Each key appears once.
    """
    keys = [key]
    if has_drift:
        keys.append(rehearsal_key(applied, pending))
    keys.extend(_destructive_prefix_keys(applied, pending, compiler))
    return list(dict.fromkeys(keys))


def _rehearsal_writes(
    compiler: "Compiler", table: str, keys: Sequence[str], outcome: str
) -> Tuple[str, str, List[Tuple[SqlValue, ...]]]:
    """
    The DELETE and INSERT statements that replace the rehearsal rows for
    these keys, and the INSERT's parameter rows. Every row gets the same
    timestamp and outcome. Every value is a non-null string, which every
    dialect's prepare_execution() passes through unchanged, so the rows
    go to executemany() as they are.
    """
    placeholder = compiler.placeholder()
    delete_sql = f"DELETE FROM {table} WHERE rehearsal_key = {placeholder}"
    values = ", ".join([placeholder] * 3)
    insert_sql = (
        f"INSERT INTO {table} (rehearsal_key, outcome, rehearsed_at) "
        f"VALUES ({values})"
    )
    stamp = datetime.now(timezone.utc).isoformat()
    rows: List[Tuple[SqlValue, ...]] = [(key, outcome, stamp) for key in keys]
    return delete_sql, insert_sql, rows


def _scratch_rehearsal_keys(
    applied: Sequence[AppliedRecord],
    pending: Sequence[Migration],
    results: "Rehearsal",
    compiler: Optional["Compiler"] = None,
) -> List[str]:
    """
    The keys a passing scratch rehearsal proves on the real database: the
    full run's key first, then the destructive prefix keys. Empty when
    the rehearsal failed, nothing is pending, or the scratch run did not
    run every pending migration, since a row would then cover statements
    nothing proved.

    A migration the rehearsal left out (up_ok is None) runs outside a
    transaction and cannot be rehearsed, so it counts as covered, the
    same way a real rehearsal's row covers it.
    """
    if not results.ok or not pending:
        return []
    proved = {r.id for r in results if r.up_ok is not False}
    if any(m.id not in proved for m in pending):
        return []
    key = rehearsal_key(applied, pending)
    prefixes = _destructive_prefix_keys(applied, pending, compiler)
    return [key] + [k for k in prefixes if k != key]


def _rehearsal_message(
    destructive: List[Tuple[str, str]],
    outcome: Optional[str],
    target: Optional[str] = None,
) -> str:
    """
    Why a run stopped, which statements stopped it, and the two ways
    forward. A failed rehearsal reads differently from no rehearsal at
    all: the operator has already seen these statements break.

    A targeted run gets the target back in the suggested command, so
    copying the line runs what was blocked and not the whole set.
    """
    target_sql = f" --target {target}" if target is not None else ""
    if outcome == REHEARSAL_FAILED:
        opening = (
            "The last rehearsal of these statements failed, and this run "
            "removes data:"
        )
    else:
        opening = (
            "This run removes data, and no rehearsal has proved these " "statements:"
        )
    width = max(len(migration_id) for migration_id, _ in destructive)
    lines = [f"  {migration_id:<{width}}  {sql}" for migration_id, sql in destructive]
    return "\n".join(
        [opening]
        + lines
        + [
            "Prove them first: sustained rehearse",
            "Or apply them without proof: sustained migrate"
            f"{target_sql} --unrehearsed",
        ]
    )


# Dialects whose schema changes roll back, so a rehearsal can undo itself.
# The others need a scratch database; see Migrator.rehearse(). DEFAULT is
# on the list for SQLite, the engine the generic compiler usually serves;
# a config that leaves the dialect unset while pointing at MySQL, whose
# DDL commits as it runs, should declare Dialects.MYSQL so the refusal
# arrives before the run instead of after it.
_REHEARSABLE = frozenset({Dialects.DEFAULT, Dialects.POSTGRES, Dialects.DUCKDB})


def _check_rehearsable(dialect: Dialects) -> None:
    """
    Refuses to rehearse where a rollback would not take the schema back.
    """
    if dialect in _REHEARSABLE:
        return
    raise ValueError(
        f"rehearse needs a database whose schema changes roll back, and "
        f"{dialect.name.lower()} is not on that list "
        f"({', '.join(sorted(d.name.lower() for d in _REHEARSABLE))}). "
        "Point rehearse at a scratch database instead: pass scratch=True "
        "on a throwaway connection, or define get_rehearsal_connection() "
        "in the config module when running the CLI."
    )


def _down_sweep(ran: List[Migration]) -> Iterator[Tuple[Migration, Optional[str]]]:
    """
    The order a rehearsal runs its down steps in, newest first, paired with
    the reason a migration cannot be proved, or None when its down step
    should run.

    A repeatable has no down step and never blocks the sweep. A versioned
    migration without one does block it: everything older sits under
    changes that cannot be taken back, so their down steps cannot run
    either.
    """
    blocked: Optional[str] = None
    for migration in reversed(ran):
        if migration.repeatable:
            yield migration, "no down step (repeatable)"
        elif blocked is not None:
            yield migration, f"down not reached: '{blocked}' has no down step"
        elif migration.down is None:
            blocked = migration.id
            yield migration, "no down step"
        else:
            yield migration, None


def _reversal_provable(
    ran: List[Migration],
    outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]],
) -> bool:
    """
    Whether comparing the schema after the down sweep against the one
    before it proves anything.

    It does when at least one down step ran and every versioned migration
    in the run reversed. A versioned migration whose down step did not run
    leaves its own changes in the database, and blaming those on the steps
    that did reverse would report a rehearsal that behaved as designed as
    a failure. Repeatables are left out of the requirement: they never
    have a down step, so waiting for one would switch the comparison off
    for every run that carries a view or a seed.
    """
    if not any(down_ok is True for down_ok, _ in outcomes.values()):
        return False
    return all(
        outcomes.get(m.id, (None, None))[0] is True for m in ran if not m.repeatable
    )


def _rehearsal_results(
    ran: List[Migration],
    up_error: Optional[Tuple[str, str]],
    down_outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]],
    landed: Optional[Dict[str, List[str]]] = None,
    reverted: Optional[List[str]] = None,
) -> List[RehearsalResult]:
    """
    Merges the up and down outcomes into one result per migration, in the
    order the up steps ran. `up_error` is the (id, message) pair of the
    migration that stopped the rehearsal, if one did.

    `landed` holds the outstanding differences per migration id, for the
    migrations whose landing was checked. `reverted` holds the schema
    left over after the down sweep, and goes on every migration whose
    down step ran, since one sweep proves them together.
    """
    landed = landed or {}
    unfinished: Tuple[Optional[bool], Optional[str]] = (
        None,
        "down not rehearsed: the run stopped",
    )
    results = [
        RehearsalResult(
            m.id,
            True,
            *down_outcomes.get(m.id, unfinished),
            landed=landed.get(m.id),
            reversed=(
                reverted if down_outcomes.get(m.id, unfinished)[0] is True else None
            ),
        )
        for m in ran
    ]
    if up_error is not None:
        results.append(RehearsalResult(up_error[0], False, None, up_error[1]))
    return results


NOT_REHEARSABLE = (
    "not rehearsed: the migration runs outside a transaction, and a "
    "rehearsal cannot roll such a run back"
)


def _skipped_results(skipped: List[Migration]) -> List[RehearsalResult]:
    """
    One unproved result per migration the rehearsal left out. A migration
    with transactional=False carries statements the engine refuses inside
    a transaction block, and the rehearsal runs inside one, so running it
    would fail a migration a real up() applies. Leaving it out keeps the
    rest of the run provable; the result says nothing was proved for it.
    """
    return [RehearsalResult(m.id, None, None, NOT_REHEARSABLE) for m in skipped]
