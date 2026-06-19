"""Regression tests for persistent state concurrency."""
import os
import tempfile
import threading
import unittest

import doctor.state


class StateTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Point the state module at a temp file for this test.
        doctor.state.STATE_FILE = os.path.join(self.tmp.name, "state.json")

    def test_transaction_loads_missing_state(self):
        with doctor.state.state_transaction() as state:
            self.assertEqual(state, {})

    def test_transaction_persists_changes(self):
        with doctor.state.state_transaction() as state:
            state["x"] = 1
        with doctor.state.state_transaction() as state:
            self.assertEqual(state.get("x"), 1)

    def test_no_lost_updates_under_concurrent_transactions(self):
        """Two threads incrementing the same counter must not clobber each other."""
        def bump():
            for _ in range(50):
                with doctor.state.state_transaction() as state:
                    state["counter"] = state.get("counter", 0) + 1

        threads = [threading.Thread(target=bump) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with doctor.state.state_transaction() as state:
            self.assertEqual(state.get("counter"), 100)


if __name__ == "__main__":
    unittest.main()
