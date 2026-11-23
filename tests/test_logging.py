import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

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
        stale_timestamp = str(int(time.time()) - 600)
        signed = self.webhook_id.encode() + b"." + stale_timestamp.encode() + b"." + self.body
        secret = base64.b64decode(main.RENDER_WEBHOOK_SECRET[6:])
        signature = base64.b64encode(
            hmac.new(secret, signed, hashlib.sha256).digest()
        ).decode("ascii")
        headers["webhook-timestamp"] = stale_timestamp
        headers["webhook-signature"] = f"v1,{signature}"
        self.assertFalse(main.verify_render_webhook(headers, self.body))

    def test_unknown_signature_version_is_rejected(self):
        headers = dict(self.headers)
        headers["webhook-signature"] = headers["webhook-signature"].replace("v1,", "v2,")
        self.assertFalse(main.verify_render_webhook(headers, self.body))


class StructuredLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._notify_state.clear()
        main._stats.update({"errors": 0, "warnings": 0, "last_error": None})

    def test_log_record_is_json_serializable(self):
        record = main.make_log_record("test.event", "WARNING", count=3)
        decoded = json.loads(json.dumps(record))
        self.assertEqual(decoded["event"], "test.event")
        self.assertEqual(decoded["level"], "WARNING")
        self.assertEqual(decoded["details"]["count"], 3)

    async def test_important_event_sends_one_compact_group_message(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("system.startup_completed", "INFO", stage="ready")

        tg_mock.assert_awaited_once()
        method, payload = tg_mock.await_args.args
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], 12345)
        self.assertIn("system.startup_completed", payload["text"])
        self.assertNotIn("rich_message", payload)

    async def test_unselected_info_and_warning_events_do_not_reach_log_group(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)) as log_id, \
             patch.object(main, "tg", new=AsyncMock()) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("ordinary.info", "INFO")
            await main.technical("ordinary.warning", "WARNING")

        log_id.assert_not_awaited()
        tg_mock.assert_not_awaited()

    def test_event_heading_cannot_inject_markdown_or_new_lines(self):
        record = main.make_log_record("bad\`event\n\`\`\`json", "WARNING")
        rendered = main.group_log_message(record)
        heading = rendered.splitlines()[0]
        self.assertNotIn("`", heading)
        self.assertNotIn("\\n", heading)
        self.assertRegex(heading, r"^WARNING · [A-Za-z0-9_.:-]+$")

    def test_only_allowlisted_metadata_is_in_group_log(self):
        record = main.make_log_record(
            "test.event",
            "ERROR",
            alias="Blue-01",
            user_id=123456789,
            message_id=555,
            direction="user_to_admin",
            message="private body must not appear",
            error="telegram temporary failure",
        )
        rendered = main.group_log_message(record)
        self.assertIn("direction: user_to_admin", rendered)
        self.assertIn("error: telegram temporary failure", rendered)
        self.assertNotIn("Blue-01", rendered)
        self.assertNotIn("123456789", rendered)
        self.assertNotIn("555", rendered)
        self.assertNotIn("private body must not appear", rendered)

    def test_oversized_log_is_truncated(self):
        record = main.make_log_record("test.large", "ERROR", error="x" * 5000)
        rendered = main.group_log_message(record)
        self.assertLessEqual(len(rendered), 900)

    async def test_missing_log_group_skips_telegram_delivery(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=None)), \
             patch.object(main, "tg", new=AsyncMock()) as tg_mock, \
             patch.object(main, "write_log_record"):
            record = await main.technical("system.startup_completed")

        self.assertEqual(record["event"], "system.startup_completed")
        tg_mock.assert_not_awaited()

    async def test_telegram_log_delivery_failure_is_not_retried_through_fallback(self):
        failure = main.IntegrationError(
            "telegram", "sendMessage", transport_error="ReadTimeout"
        )
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(side_effect=failure)) as tg_mock, \
             patch.object(main, "write_log_record") as write_log:
            await main.technical("system.startup_completed")

        tg_mock.assert_awaited_once()
        self.assertEqual(tg_mock.await_args.args[0], "sendMessage")
        self.assertEqual(write_log.call_count, 2)

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
