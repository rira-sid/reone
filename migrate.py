"""Minimal schema upgrades: create_all() makes new tables but never adds columns to existing
ones, so add any model column the live database is missing. Additive only - never drops or
alters anything. Good enough until the schema needs a real migration tool (Alembic)."""
from sqlalchemy import inspect, text

from database import Base


def add_missing_columns(engine):
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all() handles brand-new tables
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                default = ""
                if column.default is not None and column.default.is_scalar:
                    value = column.default.arg
                    if isinstance(value, bool):
                        default = f" DEFAULT {'TRUE' if value else 'FALSE'}"
                    elif isinstance(value, (int, float)):
                        default = f" DEFAULT {value}"
                    elif isinstance(value, str):
                        default = " DEFAULT '" + value.replace("'", "''") + "'"
                print(f"MIGRATE: adding {table.name}.{column.name}")
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}{default}'))
            # Indexes on columns added above (create_all only indexes brand-new tables).
            for index in table.indexes:
                index.create(conn, checkfirst=True)
