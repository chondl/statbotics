from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass, sessionmaker

from src.constants import CONN_STR, DISABLE_DB

# pool_pre_ping: db-f1-micro / the Cloud SQL proxy reaps idle connections, so a
# connection pooled across the hourly cron gap goes stale and the next query
# raises "server closed the connection unexpectedly", crashing the ETL cycle.
# pre_ping transparently reconnects; recycle proactively drops old connections.
engine = (
    None
    if DISABLE_DB
    else create_engine(CONN_STR, pool_pre_ping=True, pool_recycle=1800)
)

Session = None if DISABLE_DB else sessionmaker(bind=engine)


# Only for type hints, doesn't enable slots
# Mirror to avoid intermediate commits to DB
class Base(MappedAsDataclass, DeclarativeBase):
    pass


def clean_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(engine)
