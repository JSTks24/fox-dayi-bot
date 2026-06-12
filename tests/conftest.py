"""Shared helpers for phase 1/2 tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import gc
from contextlib import contextmanager
from pathlib import Path


class DummyBot:
    def __init__(self):
        self.user = None
        self.admins = []
        self.owner_ids = []
        self.trusted_users = []


@contextmanager
def temporary_workdir():
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        os.chdir(temp_dir)
        try:
            yield Path(temp_dir)
        finally:
            os.chdir(old_cwd)
            gc.collect()


def create_users_db(db_path: Path, *, admins: tuple[int, ...] = (), trusted_users: tuple[int, ...] = ()) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE admins (id TEXT PRIMARY KEY)")
        cursor.execute("CREATE TABLE trusted_users (id TEXT PRIMARY KEY)")
        cursor.executemany("INSERT INTO admins (id) VALUES (?)", [(str(user_id),) for user_id in admins])
        cursor.executemany(
            "INSERT INTO trusted_users (id) VALUES (?)",
            [(str(user_id),) for user_id in trusted_users],
        )
