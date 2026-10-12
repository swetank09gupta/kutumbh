"""Shared by the scratch-database tests."""
from sqlalchemy import text


def reset_schema(engine) -> None:
    """Empty the scratch database. Sessions an earlier test module left open would block DROP SCHEMA for ever,
    so they are terminated first."""
    with engine.begin() as c:
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
