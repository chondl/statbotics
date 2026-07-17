from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass, sessionmaker

from src.constants import CONN_STR

# pool_pre_ping: a pooled connection idle across the hourly-cron gap goes stale
# (Cloud SQL / db-f1-micro / the Cloud SQL proxy reap idle connections), and the
# next query raises "server closed the connection unexpectedly", 500ing the ETL
# trigger and stalling ingestion. pre_ping reconnects transparently; recycle
# drops connections older than 30 min proactively.
engine = create_engine(CONN_STR, pool_pre_ping=True, pool_recycle=1800)

Session = sessionmaker(bind=engine)


# Only for type hints, doesn't enable slots
# Mirror to avoid intermediate commits to DB
class Base(MappedAsDataclass, DeclarativeBase):
    pass


def clean_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(engine)
