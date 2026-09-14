"""Tests for the 'parent' item state and migration from 'tracking'."""

import os
import sqlite3
import tempfile
import unittest

from mahler.gh import LABEL_COLORS, LABEL_STATES, STATE_LABELS
from mahler.ledger import SCHEMA, STATES, Ledger


class ParentStateTests(unittest.TestCase):
    def test_states_membership(self):
        self.assertIn("parent", STATES)
        self.assertNotIn("tracking", STATES)

    def test_gh_label_mapping(self):
        self.assertEqual(STATE_LABELS["parent"], "mahler:parent")
        self.assertNotIn("tracking", STATE_LABELS)
        self.assertEqual(LABEL_STATES["mahler:parent"], "parent")
        # Legacy label backward compatibility
        self.assertEqual(LABEL_STATES["mahler:tracking"], "parent")
        self.assertIn("mahler:parent", LABEL_COLORS)

    def test_set_state_parent(self):
        led = Ledger(":memory:")
        led.upsert_item("testproj", 1, title="Parent issue")
        it = led.set_state("testproj", 1, "parent", why="split into sub-issues")
        self.assertEqual(it["state"], "parent")
        self.assertEqual(led.item("testproj", 1)["state"], "parent")

    def test_db_migration_from_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Seed an old database directly with 'tracking' state
            path = os.path.join(tmp, "mahler.db")
            con = sqlite3.connect(path)
            try:
                con.executescript(SCHEMA)
                con.executemany(
                    "INSERT INTO items (project, number, title, state) VALUES (?, ?, ?, ?)",
                    [("testproj", 10, "Old tracking issue", "tracking"),
                     ("testproj", 11, "Another tracking issue", "tracking"),
                     ("testproj", 12, "Plain ready issue", "ready")])
                con.commit()
            finally:
                con.close()

            # Ledger init should migrate it
            led = Ledger(path)
            try:
                self.assertEqual(led.item("testproj", 10)["state"], "parent")
                self.assertEqual(led.item("testproj", 11)["state"], "parent")
                self.assertEqual(led.item("testproj", 10)["title"], "Old tracking issue")
                # Non-tracking states must be left alone
                self.assertEqual(led.item("testproj", 12)["state"], "ready")
            finally:
                led.close()

            # Reopening the same database must not need the migration again
            led = Ledger(path)
            try:
                self.assertEqual(led.item("testproj", 10)["state"], "parent")
            finally:
                led.close()
