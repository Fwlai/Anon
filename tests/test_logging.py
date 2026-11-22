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
    def test_log_record_is_json_serializable(self):
        record = main.make_log_record("test.event", "WARNING", count=3)
        decoded = json.loads(json.dumps(record))
        self.assertEqual(decoded["event"], "test.event")
        self.assertEqual(decoded["level"], "WARNING")
        self.assertEqual(decoded["details"]["count"], 3)

    async def test_technical_sends_only_one_rich_message(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("test.event", "INFO", sample="value")
        tg_mock.assert_awaited_once()
        self.assertEqual(tg_mock.await_args.args[0], "sendRichMessage")
        self.assertIn("rich_message", tg_mock.await_args.args[1])
        self.assertNotIn("entities", tg_mock.await_args.args[1])
        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        self.assertNotIn("Anon Log", markdown)
        self.assertNotIn("**INFO**", markdown)
        self.assertIn("` test.event `", markdown)
        self.assertIn("```json", markdown)

    async def test_event_heading_cannot_inject_markdown_or_new_lines(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("bad`event\n```json", "WARNING")

        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        self.assertEqual(markdown.count("```"), 2)
        self.assertEqual(markdown.splitlines()[0], "` bad_event____json `")

    async def test_json_code_fence_inside_detail_does_not_break_markdown(self):
        original = {"snippet": "before ```json\nafter"}
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("test.event", "ERROR", payload=original)

        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        self.assertEqual(markdown.count("```"), 2)
        json_text = markdown.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        record = json.loads(json_text)
        self.assertEqual(record["details"]["payload"], original)

    async def test_oversized_log_is_truncated_before_rich_message_send(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("test.large", "ERROR", huge_value="x" * 5000)

        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        json_text = markdown.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        record = json.loads(json_text)
        self.assertTrue(record["details"]["truncated"])
        self.assertEqual(record["details"]["available_fields"], ["huge_value"])
        self.assertLess(main.utf16_length(markdown), 3100)

    async def test_missing_log_group_skips_telegram_delivery(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=None)), \
             patch.object(main, "tg", new=AsyncMock()) as tg_mock, \
             patch.object(main, "write_log_record"):
            record = await main.technical("test.no_group")

        self.assertEqual(record["event"], "test.no_group")
        tg_mock.assert_not_awaited()

    async def test_rich_message_failure_does_not_fallback_to_send_message(self):
        failure = main.IntegrationError(
            "telegram", "sendRichMessage", transport_error="ReadTimeout"
        )
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(side_effect=failure)) as tg_mock, \
             patch.object(main, "write_log_record") as write_log:
            await main.technical("test.delivery_failure")

        tg_mock.assert_awaited_once()
        self.assertEqual(tg_mock.await_args.args[0], "sendRichMessage")
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
