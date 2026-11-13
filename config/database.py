"""Classify temporary SQLite failures without logging SQL or document content."""

import sqlite3
import traceback


def transient_database_error(error):
    cause = error.__cause__ or error
    code = getattr(cause, "sqlite_errorcode", None)
    if code is not None:
        return code & 255 in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_IOERR,
        }
    return any(
        token in str(error).lower()
        for token in (
            "database is locked",
            "database table is locked",
            "database is busy",
            "unable to open database file",
            "disk i/o error",
        )
    )


def safe_failure_location(error):
    cause = error.__cause__ or error
    frames = traceback.extract_tb(error.__traceback__)
    location = ";".join(
        f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
        for frame in frames[-6:]
    )
    return (
        f"{type(error).__name__}:{getattr(cause, 'sqlite_errorname', '')}:"
        f"errno={getattr(cause, 'errno', '')}:{location}"
    )
