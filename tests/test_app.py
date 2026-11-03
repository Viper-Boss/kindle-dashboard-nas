"""Regression tests for admin auth, cache behaviour, atomic state writes and proxy config."""
import base64
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import web_auth


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        db=patch.object(web_auth,'DB_PATH',Path(self.temp.name)/'auth.db');db.start();self.addCleanup(db.stop)

    def test_correct_credentials_accepted(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "s3cret-pass"}):
            self.assertTrue(web_auth.credentials_valid("admin", "s3cret-pass"))
            self.assertFalse(web_auth.credentials_valid("admin", "wrong"))

    def test_non_ascii_credentials_do_not_raise(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "s3cret-pass"}):
            self.assertFalse(web_auth.credentials_valid("用户", "密码"))
        with patch.dict(os.environ, {"ADMIN_USERNAME": "管理员", "ADMIN_PASSWORD": "密码🔑"}):
            self.assertTrue(web_auth.credentials_valid("管理员", "密码🔑"))

    def test_placeholder_or_empty_password_is_not_a_login(self):
        for value in ("", "change-me"):
            with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": value}):
                self.assertFalse(app.admin_password_configured())
                self.assertFalse(web_auth.credentials_valid("admin", value))


class ProxyConfigTests(unittest.TestCase):
    def test_subscription_config_keeps_requested_mode(self):
        text = app.mihomo_subscription_config("https://example.com/sub?token=a&b=c", "global")
        self.assertIn("mode: global\n", text)
        self.assertIn('"https://example.com/sub?token=a&b=c"', text)

    def test_current_mode_is_read_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text("mixed-port: 7890\nmode: direct\nlog-level: warning\n", encoding="utf-8")
            with patch.object(app, "MIHOMO_CONFIG_FILE", cfg):
                self.assertEqual(app.current_proxy_mode(), "direct")
            with patch.object(app, "MIHOMO_CONFIG_FILE", Path(tmp) / "missing.yaml"):
                self.assertEqual(app.current_proxy_mode(), "rule")


class CacheTests(unittest.TestCase):
    def setUp(self):
        app.LAST_DATA, app.LAST_IMAGE, app.LAST_RENDER_TS = {}, None, 0.0
        self.calls = {"collect": 0, "render": 0}

        def collect():
            self.calls["collect"] += 1
            time.sleep(0.05)
            return {"ok": True, "prices": {"bitcoin": {"eur": 1}}, "errors": [], "kindle": {}}

        def render(data):
            self.calls["render"] += 1
            return b"png"

        patches = [patch.object(app, "collect_data", collect), patch.object(app, "render_dashboard", render),
                   patch.object(app, "setting_float", lambda name: 300.0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_cached_within_window(self):
        app.get_cached_image()
        app.get_cached_image()
        self.assertEqual(self.calls, {"collect": 1, "render": 1})

    def test_kindle_update_redraws_without_refetching_data(self):
        app.get_cached_image()
        app.LAST_IMAGE = None  # what /update does
        app.get_cached_image()
        self.assertEqual(self.calls, {"collect": 1, "render": 2})

    def test_concurrent_requests_fetch_upstream_once(self):
        threads = [threading.Thread(target=app.get_cached_image) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.calls["collect"], 1)


class StateTests(unittest.TestCase):
    def test_concurrent_state_updates_leave_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "STATE_FILE", Path(tmp) / "state.json"):
                threads = [threading.Thread(target=app.save_state, args=({"kindle_battery": str(i)},)) for i in range(20)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                state = app.load_state()
                self.assertIn("kindle_battery", state)
                self.assertFalse((Path(tmp) / "state.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
