# pyright: reportUnknownVariableType=false
"""Fetching current prices — and never letting that fetch matter to a run.

The packaged price table is dated the day the release was cut and never moves.
Mistral changes its rates every few months, so an installation left alone quietly
prices every run against figures that stopped being true — while printing them with
a date that makes them look checked.

This is the app's only network call without an API key, which is exactly why most
of these tests are about it going wrong. A filing tool must not wait on a price
server, must not trust what comes back, and must be switchable off entirely.

Served by a REAL local HTTP server rather than a mocked `urlopen`: what is being
pinned is that we survive a truncated body, an HTML error page and a hang, and a
mock of our own making would only ever return what we already expected.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from procrafiler.pricing import load_price_table
from procrafiler.pricing_refresh import (
    MAX_DOWNLOAD_BYTES,
    REFRESH_INTERVAL,
    cache_file,
    fetch_price_table,
    is_due,
    last_checked,
    pricing_url,
    refresh_enabled,
    refresh_if_due,
)

VALID = {
    "schema_version": 2,
    "providers": {
        "mistral": {
            "checked_utc": "2026-08-03T00:52:25Z",
            "updated": "2026-08-03",
            "source": "https://mistral.ai/pricing/api",
            "currency": "USD",
            "models": {
                "mistral-small-latest": {"in_per_mtok": 0.15, "out_per_mtok": 0.6},
                "voxtral-mini-latest": {"per_audio_minute": 0.003},
            },
        },
        "ovh": {
            "checked_utc": "2026-08-03T00:52:25Z",
            "updated": "2026-08-03",
            "source": "https://www.ovhcloud.com/fr/public-cloud/ai-endpoints/catalog/",
            "currency": "EUR",
            "models": {"whisper-large-v3-turbo": {"per_audio_second": 1.278e-05}},
        },
    },
}


class _Handler(BaseHTTPRequestHandler):
    body: bytes = json.dumps(VALID).encode()
    status: int = 200
    content_type: str = "application/json"
    delay: float = 0.0

    def do_GET(self) -> None:  # noqa: N802 — http.server's interface
        if _Handler.delay:
            time.sleep(_Handler.delay)
        self.send_response(_Handler.status)
        self.send_header("Content-Type", _Handler.content_type)
        self.end_headers()
        try:
            self.wfile.write(_Handler.body)
        except BrokenPipeError:
            pass  # the over-size test stops reading on purpose

    def log_message(self, *_args: object) -> None:
        pass  # keep the test output clean

    def handle_error(self, *_args: object) -> None:
        pass


class ServedTests(unittest.TestCase):
    """Everything that reaches the wire, against a real server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/pricing.json"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Handler.body = json.dumps(VALID).encode()
        _Handler.status = 200
        _Handler.content_type = "application/json"
        _Handler.delay = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name)
        os.environ["PROCRAFILER_PRICING_REFRESH"] = "on"
        os.environ["PROCRAFILER_PRICING_URL"] = self.url

    def tearDown(self) -> None:
        os.environ["PROCRAFILER_PRICING_REFRESH"] = "off"
        os.environ.pop("PROCRAFILER_PRICING_URL", None)
        self.tmp.cleanup()

    def test_a_good_file_is_fetched_and_parsed(self) -> None:
        table, text = fetch_price_table()
        assert table is not None
        self.assertEqual(table.as_of(), "2026-08-03")
        self.assertIn("voxtral-mini-latest", table.providers["mistral"].models)
        self.assertIn("voxtral", text)

    def test_each_seller_keeps_its_own_currency(self) -> None:
        """The reason the file is keyed by provider at all: Mistral publishes in
        USD and OVH in EUR, and no single top-level currency can be true for both."""
        table, _text = fetch_price_table()
        assert table is not None
        self.assertEqual(table.currency_of("mistral"), "USD")
        self.assertEqual(table.currency_of("ovh"), "EUR")

    def test_the_same_model_id_is_not_shared_between_sellers(self) -> None:
        """`mistral-small-latest` is Mistral's here. Asking OVH for it must yield
        nothing rather than Mistral's price in the wrong currency."""
        table, _text = fetch_price_table()
        assert table is not None
        self.assertIsNotNone(table.price_for("mistral", "mistral-small-latest"))
        self.assertIsNone(table.price_for("ovh", "mistral-small-latest"))

    def test_the_refreshed_table_becomes_the_one_in_force(self) -> None:
        """The whole point: what was downloaded is what prices the next run."""
        self.assertIsNotNone(refresh_if_due(self.config))
        in_force = load_price_table(self.config)
        assert in_force is not None
        self.assertEqual(in_force.as_of(), "2026-08-03")
        self.assertEqual(in_force.origin, "downloaded")

    def test_the_users_own_file_still_wins(self) -> None:
        """Someone who wrote down a negotiated rate must not have it overwritten by
        a public one, however fresh."""
        # Written in the OLD schema on purpose: someone's hand-written file is not
        # going to be reshaped every time the contract moves.
        mine = {
            "schema_version": 1, "updated": "2020-01-01", "currency": "EUR",
            "models": {"mistral-small-latest": {"in_per_mtok": 0.01}},
        }
        (self.config / "pricing.json").write_text(json.dumps(mine), encoding="utf-8")
        refresh_if_due(self.config)

        in_force = load_price_table(self.config)
        assert in_force is not None
        self.assertEqual(in_force.as_of(), "2020-01-01")
        price = in_force.price_for("mistral", "mistral-small-latest")
        assert price is not None
        self.assertEqual(price.in_per_mtok, 0.01)

    def test_an_html_error_page_is_refused(self) -> None:
        """A redirect to a login or a 404 page returns 200 with HTML. Parsed
        optimistically it would leave the app with no models and no prices."""
        _Handler.body = b"<html><body>404 not found</body></html>"
        _Handler.content_type = "text/html"
        table, _text = fetch_price_table()
        self.assertIsNone(table)

    def test_a_truncated_body_is_refused(self) -> None:
        _Handler.body = json.dumps(VALID).encode()[:40]
        self.assertIsNone(fetch_price_table()[0])

    def test_an_absurdly_large_body_is_refused(self) -> None:
        """A pricing file is a few kilobytes. Reading a gigabyte into memory before
        discovering it is not the file we asked for is the mistake."""
        _Handler.body = b"x" * (MAX_DOWNLOAD_BYTES + 10)
        self.assertIsNone(fetch_price_table()[0])

    def test_a_server_error_is_refused(self) -> None:
        _Handler.status = 500
        _Handler.body = b"server on fire"
        self.assertIsNone(fetch_price_table()[0])

    def test_a_file_with_absurd_prices_is_refused(self) -> None:
        """Bounds-checked before it can touch real money: a decimal point in the
        wrong place would multiply every forecast by a thousand.

        Parsing alone is not enough here — it succeeds, stripping every rejected
        figure to None, and yields a table that answers "I cannot price this" for
        everything. Accepting that would replace a working copy with a useless one.
        """
        _Handler.body = json.dumps({
            "schema_version": 2,
            "providers": {"mistral": {
                "currency": "USD", "updated": "2026-08-03",
                "models": {"mistral-small-latest": {"in_per_mtok": 999_999}},
            }},
        }).encode()
        self.assertIsNone(fetch_price_table()[0])

    def test_absurd_prices_at_OUR_seller_are_refused_even_beside_a_healthy_one(self) -> None:
        """The same refusal, once the published file carries sellers we never call.

        Asked whether ANY model anywhere survived the bounds check, this file
        answers yes on OVH's behalf while Mistral — the only block this
        installation reads — has been stripped to nothing. Accepting it replaces a
        working table with one that cannot price the next run. Reproduced from the
        live four-seller file before it was scoped per seller.
        """
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.addCleanup(os.environ.pop, "PROCRAFILER_AI_ANALYSIS_PRIMARY", None)

        broken = json.loads(json.dumps(VALID))
        for spec in broken["providers"]["mistral"]["models"].values():
            for unit in list(spec):
                spec[unit] = 999_999  # a decimal point in the wrong place
        _Handler.body = json.dumps(broken).encode()

        self.assertIsNone(fetch_price_table()[0])
        # And the healthy neighbour alone does not save it: OVH is untouched here.
        self.assertIsNotNone(broken["providers"]["ovh"]["models"]["whisper-large-v3-turbo"])

    def test_a_seller_we_never_call_being_broken_does_not_block_the_refresh(self) -> None:
        """Anti-vacuity: the scoped guard must not refuse every file that has a
        blemish somewhere. OVH is nobody's concern on a Mistral-only install."""
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.addCleanup(os.environ.pop, "PROCRAFILER_AI_ANALYSIS_PRIMARY", None)

        broken = json.loads(json.dumps(VALID))
        broken["providers"]["ovh"]["models"]["whisper-large-v3-turbo"]["per_audio_second"] = -1
        _Handler.body = json.dumps(broken).encode()

        table, _text = fetch_price_table()
        assert table is not None
        self.assertIsNotNone(table.price_for("mistral", "mistral-small-latest"))

    def test_a_newer_schema_is_ignored_rather_than_read_optimistically(self) -> None:
        """A field that changed meaning would be applied silently to real money."""
        _Handler.body = json.dumps(dict(VALID, schema_version=99)).encode()
        self.assertIsNone(fetch_price_table()[0])

    def test_a_slow_server_does_not_hold_up_a_run(self) -> None:
        """The promise the whole feature rests on. Filing documents must never wait
        on a price."""
        _Handler.delay = 3.0
        started = time.monotonic()
        table, _text = fetch_price_table(timeout=1)
        elapsed = time.monotonic() - started
        self.assertIsNone(table)
        self.assertLess(elapsed, 2.5, f"the fetch blocked for {elapsed:.1f}s")

    def test_a_failed_fetch_leaves_the_previous_copy_in_place(self) -> None:
        """A stale price the user can see the age of beats a wrong one."""
        refresh_if_due(self.config)
        before = cache_file(self.config).read_text("utf-8")

        _Handler.status = 500
        (self.config / "pricing.checked").unlink()
        self.assertIsNone(refresh_if_due(self.config))
        self.assertEqual(cache_file(self.config).read_text("utf-8"), before)


class SchedulingTests(unittest.TestCase):
    """When we go and look, and when we deliberately do not."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name)
        os.environ["PROCRAFILER_PRICING_REFRESH"] = "on"

    def tearDown(self) -> None:
        os.environ["PROCRAFILER_PRICING_REFRESH"] = "off"
        os.environ.pop("PROCRAFILER_PRICING_URL", None)
        self.tmp.cleanup()

    def test_the_first_run_checks(self) -> None:
        self.assertTrue(is_due(self.config))

    def test_it_does_not_check_again_the_same_day(self) -> None:
        """Prices move a few times a year; checking on every run is traffic for
        nothing."""
        with patch("procrafiler.pricing_refresh.fetch_price_table", return_value=(None, "")):
            refresh_if_due(self.config)
        self.assertFalse(is_due(self.config))

    def test_it_checks_again_after_a_week(self) -> None:
        with patch("procrafiler.pricing_refresh.fetch_price_table", return_value=(None, "")):
            refresh_if_due(self.config)
        later = datetime.now(timezone.utc) + REFRESH_INTERVAL + timedelta(minutes=1)
        self.assertTrue(is_due(self.config, now=later))

    def test_a_FAILED_attempt_still_counts_as_an_attempt(self) -> None:
        """Otherwise an offline machine retries on every single run, for ever."""
        with patch("procrafiler.pricing_refresh.fetch_price_table", return_value=(None, "")) as fetch:
            self.assertIsNone(refresh_if_due(self.config))
            self.assertIsNone(refresh_if_due(self.config))
        self.assertEqual(fetch.call_count, 1)
        self.assertIsNotNone(last_checked(self.config))

    def test_switched_off_means_the_network_is_never_touched(self) -> None:
        """Someone on an air-gapped machine should not have to trust a promise about
        how short the timeout is."""
        os.environ["PROCRAFILER_PRICING_REFRESH"] = "off"
        with patch("procrafiler.pricing_refresh.fetch_price_table") as fetch:
            self.assertIsNone(refresh_if_due(self.config))
        fetch.assert_not_called()
        self.assertFalse(is_due(self.config))
        self.assertFalse(refresh_enabled())

    def test_no_config_directory_is_not_an_error(self) -> None:
        with patch("procrafiler.pricing_refresh.fetch_price_table") as fetch:
            self.assertIsNone(refresh_if_due(None))
        fetch.assert_not_called()

    def test_an_unwritable_config_directory_is_survived(self) -> None:
        """The run must proceed; only the caching is lost."""
        with patch("pathlib.Path.write_text", side_effect=OSError("read-only")), \
             patch("procrafiler.pricing_refresh.fetch_price_table", return_value=(None, "")):
            self.assertIsNone(refresh_if_due(self.config))

    def test_a_corrupt_stamp_is_treated_as_never_checked(self) -> None:
        (self.config / "pricing.checked").write_text("last tuesday", encoding="utf-8")
        self.assertIsNone(last_checked(self.config))
        self.assertTrue(is_due(self.config))


class ConfigurationTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_PRICING_URL", None)
        os.environ["PROCRAFILER_PRICING_REFRESH"] = "off"

    def test_the_default_url_is_the_companion_repository(self) -> None:
        os.environ.pop("PROCRAFILER_PRICING_URL", None)
        self.assertTrue(pricing_url().startswith("https://"))
        self.assertIn("ai-pricing", pricing_url())

    def test_the_url_can_be_pointed_elsewhere(self) -> None:
        os.environ["PROCRAFILER_PRICING_URL"] = "https://example.invalid/p.json"
        self.assertEqual(pricing_url(), "https://example.invalid/p.json")

    def test_every_way_of_saying_no_is_understood(self) -> None:
        for value in ("off", "0", "false", "no", "OFF", "False"):
            with self.subTest(value=value):
                os.environ["PROCRAFILER_PRICING_REFRESH"] = value
                self.assertFalse(refresh_enabled())

    def test_it_is_on_by_default(self) -> None:
        """The suite forces it off in `tests/__init__`; unset, it is on."""
        os.environ.pop("PROCRAFILER_PRICING_REFRESH", None)
        self.assertTrue(refresh_enabled())


class SuiteIsOfflineTests(unittest.TestCase):
    def test_the_routine_suite_never_reaches_the_network(self) -> None:
        """This is the app's only network call without an API key, so the `.env`
        guard that keeps the suite offline does not cover it. A run started by any
        test would otherwise find no stamp, decide it is due, and call GitHub."""
        self.assertEqual(os.environ.get("PROCRAFILER_PRICING_REFRESH"), "off")


if __name__ == "__main__":
    unittest.main()
