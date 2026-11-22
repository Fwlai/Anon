import base64
import hashlib
import hmac
import json
import time
import unittest

import main


class RenderWebhookVerificationTests(unittest.TestCase):
    def setUp(self):
        secret = b"anon-render-webhook-test-secret"
        main.RENDER_WEBHOOK_SECRET = "whsec_" + base64.b64encode(secret).decode("ascii")
        self.webhook_id = "evt-12345678901234567890"
        self.timestamp = str(int(time.time()))
        self.body = b'{"type":"deploy_ended","timestamp":"2026-11-22T00:00:00Z","data":{"id":"evt-12345678901234567890","serviceId":"srv-test","serviceName":"anon","status":"succeeded"}}'
        signed = self.webhook_id.encode() + b"." + self.timestamp.encode() + b"." + self.body
        signature = base64.b64encode(
            hmac.new(secret, signed, hashlib.sha256).digest()
        ).decode("ascii")
        self.headers = {
            "webhook-id": self.webhook_id,
            "webhook-timestamp": self.timestamp,
            "webhook-signature": f"v1,{signature}",
        }

    def test_valid_signature(self):
        self.assertTrue(main.verify_render_webhook(self.headers, self.body))

    def test_tampered_body_is_rejected(self):
        self.assertFalse(main.verify_render_webhook(self.headers, self.body + b" "))

    def test_wrong_signature_is_rejected(self):
        headers = dict(self.headers)
        headers["webhook-signature"] = "v1,ZmFrZQ=="
        self.assertFalse(main.verify_render_webhook(headers, self.body))

    def test_stale_timestamp_is_rejected(self):
        headers = dict(self.headers)
        headers["webhook-timestamp"] = str(int(time.time()) - 600)
        self.assertFalse(main.verify_render_webhook(headers, self.body))

    def test_unknown_signature_version_is_rejected(self):
        headers = dict(self.headers)
        headers["webhook-signature"] = headers["webhook-signature"].replace("v1,", "v2,")
        self.assertFalse(main.verify_render_webhook(headers, self.body))


class StructuredLogTests(unittest.TestCase):
    def test_log_record_is_json_serializable(self):
        record = main.make_log_record("test.event", "WARNING", count=3)
        decoded = json.loads(json.dumps(record))
        self.assertEqual(decoded["event"], "test.event")
        self.assertEqual(decoded["level"], "WARNING")
        self.assertEqual(decoded["details"]["count"], 3)

    def test_render_private_fields_are_removed(self):
        result = main.sanitize_render_details({
            "deploy": {"status": "failed"},
            "trigger": {"user": {"email": "private@example.com", "id": "user-id"}},
        })
        self.assertEqual(result["deploy"]["status"], "failed")
        self.assertNotIn("trigger", result)
        self.assertNotIn("private@example.com", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
