from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

# DB retirement Phase 4: the relational engine/Session and clean_db are gone —
# there is no database. SQLAlchemy survives ONLY as the declarative base for
# src/db/models/, which stays load-bearing db-less: the Parquet writer, the
# state snapshot, and the DuckDB schemas all introspect these classes for
# column names and types.


# Only for type hints, doesn't enable slots
class Base(MappedAsDataclass, DeclarativeBase):
    pass
