"""Database transaction helper.

Provides a single ``run_transaction`` entry point used across ``src/db``.

Historically the codebase imported ``run_transaction`` directly from
``sqlalchemy_cockroachdb``. That helper is CockroachDB-specific (it rewrites
savepoint names for CRDB's transaction-retry protocol). To let the backend run
against plain PostgreSQL (e.g. Cloud SQL) as well as CockroachDB, this wrapper
dispatches on the engine dialect:

* CockroachDB  -> delegate to ``sqlalchemy_cockroachdb.run_transaction`` so
  production behavior is byte-for-byte unchanged (imported lazily so a
  Postgres-only deployment need not install the CRDB dialect).
* everything else (PostgreSQL) -> a plain SQLAlchemy transaction with the same
  retry-on-serialization-failure semantics (SQLSTATE 40001).

Call signature matches the original: ``run_transaction(Session, callback)``
where ``Session`` is a ``sessionmaker`` and ``callback(session)`` performs the
work and returns a value. ``callback`` must not commit or roll back; it may be
invoked more than once and so must be free of non-DB side effects.
"""
from typing import Any, Callable, Optional

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.session import Session as SessionType

from src.db.main import engine

# PostgreSQL / CockroachDB serialization failure (retryable).
SERIALIZATION_FAILURE = "40001"

# Default retry budget for the plain-Postgres path (CRDB helper defaults to
# unbounded; a small bounded budget is friendlier for a single-writer pipeline).
DEFAULT_MAX_RETRIES = 3


def _run_plain(
    sessionmaker: Any,
    callback: Callable[[SessionType], Any],
    max_retries: int,
) -> Any:
    retry_count = 0
    while True:
        session = sessionmaker()
        try:
            with session.begin():
                return callback(session)
        except DBAPIError as exc:
            retryable = getattr(exc.orig, "pgcode", None) == SERIALIZATION_FAILURE
            if retryable and retry_count < max_retries:
                retry_count += 1
                continue
            raise
        finally:
            session.close()


def run_transaction(
    transactor: Any,
    callback: Callable[[SessionType], Any],
    max_retries: Optional[int] = None,
    max_backoff: int = 0,
) -> Any:
    if engine.dialect.name == "cockroachdb":
        import sqlalchemy_cockroachdb

        return sqlalchemy_cockroachdb.run_transaction(
            transactor, callback, max_retries=max_retries, max_backoff=max_backoff
        )

    return _run_plain(
        transactor,
        callback,
        DEFAULT_MAX_RETRIES if max_retries is None else max_retries,
    )
