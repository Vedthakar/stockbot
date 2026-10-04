"""Safety defaults a stranger relies on: paper unless --live + typed phrase,
optional judge, per-strategy toggles. No network calls."""

import io
import os
import unittest
from unittest import mock

import judge
import main
import strategies
from broker import DEFAULT_PAPER_URL, Broker

FAKE_KEYS = {"ALPACA_API_KEY": "test-key", "ALPACA_SECRET_KEY": "test-secret"}


class BrokerModeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, FAKE_KEYS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # never read a developer's real .env during tests
        dotenv = mock.patch("broker.load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)

    def test_paper_is_default(self):
        b = Broker({})
        self.assertTrue(b.paper)
        self.assertEqual(b.trading._base_url, DEFAULT_PAPER_URL)

    def test_endpoint_override_from_env(self):
        with mock.patch.dict(os.environ, {"ALPACA_PAPER_URL": "https://example.test"}):
            self.assertEqual(Broker({}).trading._base_url, "https://example.test")

    def test_config_cannot_switch_to_live(self):
        with self.assertRaises(SystemExit):
            Broker({"account": {"mode": "live"}})

    def test_missing_keys_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            Broker({})


class LiveConfirmTest(unittest.TestCase):
    def test_flag_defaults_off(self):
        args = main.parse_args([])
        self.assertFalse(args.live)
        self.assertFalse(args.no_judge)

    def test_non_interactive_refuses(self):
        with mock.patch("sys.stdin", io.StringIO(main.LIVE_CONFIRM_PHRASE + "\n")):
            self.assertFalse(main.confirm_live_trading())

    def test_wrong_phrase_refuses(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="yes"):
            self.assertFalse(main.confirm_live_trading())

    def test_exact_phrase_confirms(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value=main.LIVE_CONFIRM_PHRASE):
            self.assertTrue(main.confirm_live_trading())


class JudgeOptionalTest(unittest.TestCase):
    def test_disabled_judge_approves_without_network(self):
        with mock.patch("judge.requests.post") as post:
            ok, reason = judge.veto_check({"enabled": False}, "summary", "")
        post.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(reason, judge.RULES_ONLY_REASON)

    def test_disabled_judge_writes_no_learnings(self):
        self.assertIsNone(judge.write_learnings_entry({"enabled": False}, "facts"))

    def test_enabled_unreachable_judge_fails_closed(self):
        cfg = {"enabled": True, "base_url": "http://127.0.0.1:9", "model": "m",
               "timeout_seconds": 1}
        ok, reason = judge.veto_check(cfg, "summary", "")
        self.assertFalse(ok)
        self.assertIn("judge unavailable", reason)


class StrategyTogglesTest(unittest.TestCase):
    def test_missing_block_means_all_off(self):
        self.assertFalse(any(strategies.toggles({}).values()))

    def test_each_strategy_toggles_independently(self):
        on = strategies.toggles({"strategies": {"higher_low": True, "ob_fvg": True}})
        self.assertEqual({k for k, v in on.items() if v}, {"higher_low", "ob_fvg"})

    def test_shipped_config_names_every_strategy(self):
        import yaml
        with open(os.path.join(main.ROOT, "config.yaml")) as f:
            cfg = yaml.safe_load(f)
        self.assertEqual(set(cfg["strategies"]), set(strategies.STRATEGY_NAMES))
        self.assertNotIn("account", cfg)


if __name__ == "__main__":
    unittest.main()
