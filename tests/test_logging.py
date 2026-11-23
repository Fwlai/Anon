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

    def test_log_record_is_json_serializable(self):
        record = main.make_log_record("test.event", "WARNING", count=3)
        decoded = json.loads(json.dumps(record))
        self.assertEqual(decoded["event"], "test.event")
        self.assertEqual(decoded["level"], "WARNING")
        self.assertEqual(decoded["details"]["count"], 3)

    def test_log_record_hides_numeric_ids_and_platform_metadata(self):
        record = main.make_log_record(
            "test.privacy", "ERROR",
            sender_user_id=5272351424,
            recipient_user_id=6530793575,
            alias="Blue-01",
            error="chat 5272351424 not found",
            nested={"chat_id": 123456789, "message_id": 42},
        )
        dumped = json.dumps(record)
        self.assertNotIn("5272351424", dumped)
        self.assertNotIn("6530793575", dumped)
        self.assertNotIn("123456789", dumped)
        self.assertEqual(record["details"]["alias"], "Blue-01")
        self.assertEqual(record["details"]["nested"], {"message_id": 42})
        for hidden in ("render", "service", "environment", "request_id"):
            self.assertNotIn(hidden, record)

    def test_only_problems_and_notable_events_reach_the_group(self):
        self.assertFalse(main.should_notify_group("message.forwarded_to_admin", "INFO"))
        self.assertFalse(main.should_notify_group("message.received", "INFO"))
        self.assertTrue(main.should_notify_group("message.delivery_rejected", "ERROR"))
        self.assertTrue(main.should_notify_group("system.startup_completed", "INFO"))
        self.assertTrue(main.should_notify_group("system.shutdown_started", "INFO"))

    async def test_routine_info_event_is_not_sent_to_the_group(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock()) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("message.forwarded_to_admin", "INFO", alias="Blue-01")

        tg_mock.assert_not_awaited()

    async def test_repeated_error_is_sent_once_per_cooldown(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            for _ in range(3):
                await main.technical("test.repeat", "ERROR")

        tg_mock.assert_awaited_once()

    async def test_technical_sends_only_one_rich_message(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("test.event", "ERROR", sample="value")
        tg_mock.assert_awaited_once()
        self.assertEqual(tg_mock.await_args.args[0], "sendRichMessage")
        self.assertIn("rich_message", tg_mock.await_args.args[1])
        self.assertNotIn("entities", tg_mock.await_args.args[1])
        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        self.assertTrue(markdown.startswith(
            "<details><summary><code>test.event</code></summary>\n\n```json\n"
        ))
        self.assertTrue(markdown.endswith("\n```\n</details>"))
        self.assertNotIn("Anon Log", markdown)
        self.assertNotIn("**INFO**", markdown)

    async def test_event_heading_cannot_inject_markdown_or_new_lines(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("bad`event\n```json", "WARNING")

        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        self.assertEqual(markdown.count("```"), 2)
        self.assertTrue(markdown.startswith(
            "<details><summary><code>bad_event____json</code></summary>"
        ))
        self.assertTrue(markdown.endswith("\n```\n</details>"))

    async def test_json_code_fence_inside_detail_does_not_break_markdown(self):
        original = {"snippet": "before ```json\nafter"}
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("test.event", "ERROR", payload=original)

        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        self.assertEqual(markdown.count("```"), 2)
        json_text = markdown.split("```json\n", 1)[1].rsplit("\n```\n</details>", 1)[0]
        record = json.loads(json_text)
        self.assertEqual(record["details"]["payload"], original)

    async def test_oversized_log_is_truncated_before_rich_message_send(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \
             patch.object(main, "write_log_record"):
            await main.technical("test.large", "ERROR", huge_value="x" * 5000)

        markdown = tg_mock.await_args.args[1]["rich_message"]["markdown"]
        json_text = markdown.split("```json\n", 1)[1].rsplit("\n```\n</details>", 1)[0]
        record = json.loads(json_text)
        self.assertTrue(record["details"]["truncated"])
        self.assertEqual(record["details"]["available_fields"], ["huge_value"])
        self.assertLess(main.utf16_length(markdown), 3100)

    async def test_missing_log_group_skips_telegram_delivery(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=None)), \
             patch.object(main, "tg", new=AsyncMock()) as tg_mock, \
             patch.object(main, "write_log_record"):
            record = await main.technical("test.no_group", "ERROR")

        self.assertEqual(record["event"], "test.no_group")
        tg_mock.assert_not_awaited()

    async def test_rich_message_failure_does_not_fallback_to_send_message(self):
        failure = main.IntegrationError(
            "telegram", "sendRichMessage", transport_error="ReadTimeout"
        )
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \
             patch.object(main, "tg", new=AsyncMock(side_effect=failure)) as tg_mock, \
             patch.object(main, "write_log_record") as write_log:
            await main.technical("test.delivery_failure", "ERROR")

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


class HealthCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_report_summarizes_state_without_user_ids(self):
        async def fake_tg(method, payload):
            return {"pending_update_count": 0}

        with patch.object(main, "db_get", new=AsyncMock(return_value=[])), \
             patch.object(main, "tg", new=AsyncMock(side_effect=fake_tg)):
            report = await main.build_health_report()

        self.assertTrue(report.startswith("✅ Anon is healthy"))
        self.assertIn("Database: ✅", report)
        self.assertIn("Telegram webhook: ✅ 0 pending", report)
        self.assertIn("Uptime:", report)

    async def test_health_in_admin_private_chat_is_never_relayed(self):
        message = {"message_id": 700, "text": "/health"}
        with patch.object(main, "react", new=AsyncMock()) as react:
            handled = await main.admin_command(message)

        self.assertTrue(handled)
        react.assert_awaited_once_with(main.ADMIN_ID, 700, "👎")


if __name__ == "__main__":
    unittest.main()
