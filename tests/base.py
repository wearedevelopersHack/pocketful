"""Shared test case for scenarios that run against the real `ledger/` package."""

import os
import sys
import tempfile
import unittest
import uuid

from tests.harness import RealBackend


class RealLedgerCase(unittest.TestCase):
    """Base for every test that exercises the real ledger.

    The ledger is built per test. If `ledger.core.Ledger` does not exist yet, the
    test **fails** with a clear message rather than skipping: a skipped test would
    make a gate green against an absence of implementation, which is the exact
    way a suite launders a missing feature.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.backend = self.new_backend()

    def new_backend(self):
        path = os.path.join(self._tmp.name, f"ledger-{uuid.uuid4().hex}.db")
        backend = RealBackend(path).prepare()
        self.addCleanup(self._report_provenance, backend)
        return backend

    def _report_provenance(self, backend):
        # Printed into the test log so the evidence says exactly which
        # connection factory and schema the run exercised.
        print(
            f"\n[provenance] connections: {backend._conn_factory_source}\n"
            f"[provenance] schema:      {backend._schema_source}",
            file=sys.stderr,
        )
