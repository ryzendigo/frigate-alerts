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


class SilentZonesAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._saved = main.config
        main.config = {}

    def tearDown(self):
        main.config = self._saved

    def test_silent_zone_all_silent(self):
        main.config = {"silent_zones": ["street", "sidewalk"]}
        self.assertTrue(main.zones_are_silent(["street"]))
        self.assertTrue(main.zones_are_silent(["street", "sidewalk"]))

    def test_silent_zone_mixed_is_loud(self):
        main.config = {"silent_zones": ["street"]}
        self.assertFalse(main.zones_are_silent(["street", "driveway"]))

    def test_silent_zone_none_configured(self):
        main.config = {}
        self.assertFalse(main.zones_are_silent(["street"]))

    def test_silent_zone_empty_event_zones(self):
        main.config = {"silent_zones": ["street"]}
        self.assertFalse(main.zones_are_silent([]))

    def test_snapshot_query_default_bbox(self):
        main.config = {}
        self.assertEqual(main._snapshot_query(), "?bbox=1")

    def test_snapshot_query_all_options(self):
        main.config = {"snapshot": {"bbox": True, "timestamp": True, "crop": True}}
        self.assertEqual(main._snapshot_query(), "?bbox=1&timestamp=1&crop=1")

    def test_snapshot_query_all_off(self):
        main.config = {"snapshot": {"bbox": False}}
        self.assertEqual(main._snapshot_query(), "")


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

    def test_prom_escape(self):
        self.assertEqual(main._prom_escape("front"), "front")
        self.assertEqual(main._prom_escape('bad"name'), 'bad\\"name')
        self.assertEqual(main._prom_escape("a\\b"), "a\\\\b")
        self.assertEqual(main._prom_escape("line1\nline2"), "line1\\nline2")


class LLMTests(unittest.TestCase):
    def test_returns_none_without_required_config(self):
        from app.llm import describe_image
        self.assertIsNone(describe_image({}, b"img"))
        self.assertIsNone(describe_image({"base_url": "x"}, b"img"))   # no model
        self.assertIsNone(describe_image({"base_url": "x", "model": "m"}, b""))  # no image

    def test_success_parses_description_and_builds_request(self):
        from app.llm import describe_image

        class MockResp:
            status_code = 200
            text = ""
            def json(self):
                return {"choices": [{"message": {"content": "A person is walking a dog."}}]}

        class MockSess:
            def post(self, url, **kw):
                self.url, self.kw = url, kw
                return MockResp()

        s = MockSess()
        out = describe_image({"base_url": "http://x/v1", "model": "llava", "api_key": "k"},
                             b"imgbytes", context="person on front", session=s)
        self.assertEqual(out, "A person is walking a dog.")
        self.assertTrue(s.url.endswith("/chat/completions"))
        body = s.kw["json"]
        self.assertEqual(body["model"], "llava")
        self.assertEqual(body["messages"][0]["content"][1]["type"], "image_url")
        self.assertEqual(s.kw["headers"]["Authorization"], "Bearer k")

    def test_fail_open_on_exception(self):
        from app.llm import describe_image

        class MockSess:
            def post(self, url, **kw):
                raise RuntimeError("boom")

        self.assertIsNone(describe_image({"base_url": "http://x/v1", "model": "m"},
                                         b"img", session=MockSess()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
