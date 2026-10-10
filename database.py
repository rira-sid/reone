import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./riraone.db")
if DATABASE_URL.startswith("postgres://"):  # SQLAlchemy only accepts the postgresql:// scheme
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # pool_pre_ping: Neon suspends idle databases and drops their connections - test before use.
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
