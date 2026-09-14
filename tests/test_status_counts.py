from __future__ import annotations

import sqlite3
import unittest

from catalog_app.scan_store import active_table_counts


class ActiveTableCountsTests(unittest.TestCase):
    def test_counts_total_and_available_rows_in_one_query(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE media_files (id INTEGER PRIMARY KEY, is_available INTEGER NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO media_files (is_available) VALUES (?)",
                [(1,), (0,), (1,)],
            )
            statements: list[str] = []
            connection.set_trace_callback(statements.append)

            counts = active_table_counts(connection, "media_files")

            self.assertEqual((3, 2), counts)
            selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
            self.assertEqual(1, len(selects))
        finally:
            connection.close()

    def test_empty_table_returns_zero_counts(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE folders (id INTEGER PRIMARY KEY, is_available INTEGER NOT NULL)"
            )

            self.assertEqual((0, 0), active_table_counts(connection, "folders"))
        finally:
            connection.close()

    def test_rejects_dynamic_table_names(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(ValueError):
                active_table_counts(connection, "scan_media_files")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
