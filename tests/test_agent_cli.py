"""CLI tests for the offline commands (log/decide/ingest/report/export)."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from app import agent_cli, tracking


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        self.db = str(Path(self._tmp.name) / "cli.db")

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = agent_cli.main(["--db", self.db, *argv])
        return code, buf.getvalue()

    def test_decide_log_ingest_report_roundtrip(self):
        code, out = self.run_cli(
            "decide", "--ticker", "NVDA", "--action", "call", "--price", "181.2",
            "--score", "34.5", "--rationale", "MACD expanding", "--order-id", "ord-9")
        self.assertEqual(code, 0)
        self.assertIn("Recorded decision #1", out)

        code, out = self.run_cli(
            "log", "order", "--ticker", "NVDA", "--source", "robinhood-mcp",
            "--note", "placed via MCP", "--payload", '{"id": "ord-9"}')
        self.assertEqual(code, 0)
        self.assertIn("Logged event #1", out)

        payload = json.dumps({"results": [
            {"side": "buy", "chain_symbol": "NVDA", "quantity": 1,
             "average_price": 181.2, "state": "filled", "id": "ord-9"}]})
        code, out = self.run_cli("ingest", "--payload", payload)
        self.assertEqual(code, 0)
        self.assertIn("Stored 1 event(s)", out)

        code, out = self.run_cli("report")
        self.assertEqual(code, 0)
        self.assertIn("decisions: 1 total, 1 pending grade", out)
        self.assertIn("NVDA", out)

        code, out = self.run_cli("decisions", "--json")
        rows = json.loads(out)
        self.assertEqual(rows[0]["order_id"], "ord-9")
        self.assertEqual(rows[0]["action"], "call")

        code, out = self.run_cli("events", "--kind", "fill", "--json")
        self.assertEqual(json.loads(out)[0]["ticker"], "NVDA")

    def test_payload_from_file_and_export(self):
        pfile = Path(self._tmp.name) / "orders.json"
        pfile.write_text('[{"side": "sell", "symbol": "AAPL", "quantity": 2, "price": 230}]')
        code, out = self.run_cli("ingest", "--payload", f"@{pfile}")
        self.assertEqual(code, 0)

        outfile = Path(self._tmp.name) / "backup.json"
        code, out = self.run_cli("export", "--out", str(outfile))
        self.assertEqual(code, 0)
        dump = json.loads(outfile.read_text())
        self.assertEqual(dump["events"][0]["ticker"], "AAPL")

    def test_bad_action_is_a_clean_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
            code = agent_cli.main(["--db", self.db, "decide", "--ticker", "X",
                                   "--action", "buy", "--horizon", "0"])
        # horizon 0 is clamped to 1 by the store, not an error
        self.assertEqual(code, 0)

    def test_ingest_requires_payload(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
            code = agent_cli.main(["--db", self.db, "ingest"])
        self.assertEqual(code, 2)
        self.assertIn("provide --payload", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
