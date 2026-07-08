"""Regression tests for the bug-prone core logic.

Run with stdlib only (no pytest needed):
    python tests/test_core.py

Covers the areas that have actually had bugs: secret masking/unmasking,
camera/label/zone filtering, message templating, and the provider-error check.
Importing app.main creates the thread pools and FastAPI app but does NOT start
MQTT/poller/threads (that happens in the lifespan, which unittest never runs).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import main  # noqa: E402


class MaskUnmaskTests(unittest.TestCase):
    RYAN = "ui4s1239pzkeyeoory88s8hdj4g1zd"
    TRISH = "ug83wo2rdgmwp38e7ww7w8v1nu28jh"
    WH1 = "https://discord.com/api/webhooks/111111111111111111/AAAtoken_one_xxxxxxxxxxxxxxxx"
    WH2 = "https://discord.com/api/webhooks/222222222222222222/BBBtoken_two_yyyyyyyyyyyyyyyy"

    def _orig(self):
        return {
            "mqtt": {"password": "mqttpassword", "username": "mqtt", "server": "10.0.0.1"},
            "pushover": {"token": "aqzgtnxn1stpc6t37tvq725zipprm7",
                         "recipients": [{"name": "Ryan", "userkey": self.RYAN},
                                        {"name": "Trish", "userkey": self.TRISH}]},
            "discord": {"webhooks": [{"name": "a", "url": self.WH1}, {"name": "b", "url": self.WH2}]},
            "smtp": {"password": "", "username": ""},
        }

    def test_noop_roundtrip_exact(self):
        orig = self._orig()
        masked = main._mask_secrets(orig)
        self.assertEqual(main._unmask_secrets(copy.deepcopy(masked), orig), orig)

    def test_two_webhooks_mask_uniquely(self):
        masked = main._mask_secrets(self._orig())
        urls = [w["url"] for w in masked["discord"]["webhooks"]]
        self.assertNotEqual(urls[0], urls[1], "webhooks with shared prefix must not mask identically")

    def test_delete_webhook_keeps_correct_url(self):
        orig = self._orig()
        masked = main._mask_secrets(orig)
        masked["discord"]["webhooks"] = [masked["discord"]["webhooks"][1]]  # keep only 'b'
        out = main._unmask_secrets(masked, orig)
        self.assertEqual(out["discord"]["webhooks"][0]["url"], self.WH2)

    def test_reorder_recipients_keeps_keys(self):
        orig = self._orig()
        masked = main._mask_secrets(orig)
        masked["pushover"]["recipients"].reverse()
        out = main._unmask_secrets(masked, orig)
        keys = {r["name"]: r["userkey"] for r in out["pushover"]["recipients"]}
        self.assertEqual(keys, {"Ryan": self.RYAN, "Trish": self.TRISH})

    def test_edited_secret_is_kept(self):
        orig = self._orig()
        masked = main._mask_secrets(orig)
        masked["pushover"]["recipients"][1]["userkey"] = "brand_new_real_key"
        out = main._unmask_secrets(masked, orig)
        self.assertEqual(out["pushover"]["recipients"][0]["userkey"], self.RYAN)
        self.assertEqual(out["pushover"]["recipients"][1]["userkey"], "brand_new_real_key")

    def test_empty_secret_untouched(self):
        orig = self._orig()
        masked = main._mask_secrets(orig)
        self.assertEqual(main._unmask_secrets(masked, orig)["smtp"]["password"], "")

    def test_no_masked_placeholder_survives(self):
        orig = self._orig()
        masked = main._mask_secrets(orig)
        out = main._unmask_secrets(copy.deepcopy(masked), orig)
        flat = repr(out)
        self.assertNotIn("****", flat)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self._saved = main.config
        main.config = {}

    def tearDown(self):
        main.config = self._saved

    def test_camera_filter(self):
        main.config = {"cameras": ["front"], "labels": [], "zones": {}}
        self.assertTrue(main.should_notify("front", ["person"], []))
        self.assertFalse(main.should_notify("back", ["person"], []))

    def test_label_filter(self):
        main.config = {"cameras": [], "labels": ["person"], "zones": {}}
        self.assertTrue(main.should_notify("front", ["person", "car"], []))
        self.assertFalse(main.should_notify("front", ["car"], []))

    def test_empty_filters_allow_all(self):
        main.config = {"cameras": [], "labels": [], "zones": {}}
        self.assertTrue(main.should_notify("anything", ["cat"], ["anywhere"]))

    def test_zone_block(self):
        main.config = {"zones": {"allow": [], "block": ["street"]}}
        self.assertFalse(main.check_zones(["street"]))
        self.assertTrue(main.check_zones(["driveway"]))

    def test_zone_allow(self):
        main.config = {"zones": {"allow": ["driveway"], "block": []}}
        self.assertTrue(main.check_zones(["driveway"]))
        self.assertFalse(main.check_zones(["street"]))

    def test_filter_labels(self):
        main.config = {"labels": ["person"]}
        self.assertEqual(main.filter_labels(["person", "car"]), ["person"])
        main.config = {"labels": []}
        self.assertEqual(main.filter_labels(["person", "car"]), ["person", "car"])


class TemplateAndMiscTests(unittest.TestCase):
    def setUp(self):
        self._saved = main.config
        main.config = {}

    def tearDown(self):
        main.config = self._saved

    def test_render_template_ok(self):
        self.assertEqual(main.render_template("{label} on {camera}", {"label": "Person", "camera": "front"}),
                         "Person on front")

    def test_render_template_missing_var_returns_template(self):
        self.assertEqual(main.render_template("{nope}", {"label": "x"}), "{nope}")

    def test_build_message_default(self):
        main.config = {}
        title, message = main.build_message("front", "person", "", "r1", "e1")
        self.assertIn("front", message)
        self.assertIn("Person", title)

    def test_build_message_face_names(self):
        main.config = {}
        title, message = main.build_message("front", "person", "", "r1", "e1", face_names=["Alice"])
        self.assertIn("Alice", message)

    def test_is_error_result(self):
        self.assertTrue(main._is_error_result("error: 429"))
        self.assertTrue(main._is_error_result(("error: 500", None)))
        self.assertFalse(main._is_error_result("sent"))
        self.assertFalse(main._is_error_result(("sent", "12345")))

    def test_quiet_hours_disabled(self):
        main.config = {"quiet_hours": {"enabled": False}}
        self.assertFalse(main.in_quiet_hours())


if __name__ == "__main__":
    unittest.main(verbosity=2)
