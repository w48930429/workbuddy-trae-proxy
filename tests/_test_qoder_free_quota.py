"""Qoder 免费模型（QoDuck / Flash）目录常驻与额度探测单元测试。"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_QODER_DIR = os.path.join(_ROOT, "qoder")
if _QODER_DIR not in sys.path:
    sys.path.insert(0, _QODER_DIR)

import qoder_route
import qoder_pool as pool


class QoderFreeQuotaTests(unittest.TestCase):
    def test_qoduck_is_recognized_as_promo_model(self):
        self.assertTrue(qoder_route.is_flash_promo_model("qoduck"))
        self.assertTrue(qoder_route.is_flash_promo_model("QoDuck"))
        self.assertTrue(qoder_route.is_flash_promo_model("lmodel"))
        self.assertTrue(qoder_route.is_flash_promo_model("qwen3.8-flash"))
        self.assertFalse(qoder_route.is_flash_promo_model("dmodel"))

    def test_qoder_models_injects_qoduck_when_missing(self):
        with patch.object(qoder_route, "get_pool") as mock_pool:
            p = MagicMock()
            p.snapshot.return_value = []
            p.size.return_value = 0
            mock_pool.return_value = p
            models = qoder_route.qoder_models()

        qoduck = next((m for m in models if m.get("id") == "QoDuck"), None)
        self.assertIsNotNone(qoduck)
        self.assertEqual(qoduck.get("qoder_key"), "lmodel")
        self.assertEqual(qoduck.get("price_factor"), 0.0)

    def test_probe_free_quota_detects_exhausted_slot(self):
        fake_slot = pool.Slot(id="test_s1", name="Test Account", region="cn")
        fake_slot.mark_flash_free_exhausted()

        with patch.object(qoder_route, "get_pool") as mock_pool:
            p = MagicMock()
            p.snapshot.return_value = [fake_slot]
            p.size.return_value = 1
            mock_pool.return_value = p

            res = qoder_route.probe_free_quota(realm="cn", model="qoduck")

        self.assertTrue(res["ok"])
        self.assertEqual(res["summary"]["total"], 1)
        self.assertEqual(res["summary"]["exhausted"], 1)
        self.assertEqual(res["summary"]["available"], 0)
        self.assertTrue(res["results"][0]["exhausted"])
        self.assertTrue(res["results"][0]["cached"])

    def test_probe_free_quota_success(self):
        fake_slot = pool.Slot(id="test_s2", name="Active Account", region="cn")

        with patch.object(qoder_route, "get_pool") as mock_pool, \
             patch.object(qoder_route, "stream_with_slot", return_value=(True, None)):
            p = MagicMock()
            p.snapshot.return_value = [fake_slot]
            p.size.return_value = 1
            mock_pool.return_value = p

            res = qoder_route.probe_free_quota(realm="cn", model="qoduck")

        self.assertTrue(res["ok"])
        self.assertEqual(res["summary"]["total"], 1)
        self.assertEqual(res["summary"]["available"], 1)
        self.assertEqual(res["summary"]["exhausted"], 0)
        self.assertEqual(res["results"][0]["status"], "available")
        self.assertFalse(res["results"][0]["exhausted"])


if __name__ == "__main__":
    unittest.main()
