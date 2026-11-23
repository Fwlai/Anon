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

    def test_log_record_redacts_ids_aliases_and_platform_metadata(self):
        record = main.make_log_record(
            "test.privacy", "ERROR",
            sender_user_id=5272351424, recipient_user_id=6530793575,
            alias="Blue-01", service="anon", repo="Fwlai/Anon",
            branch="main", commit="abcdef123",
            error="chat 5272351424 not found",
            nested={"chat_id": 123456789, "message_id": 42},
        )
        dumped = json.dumps(record)
        for hidden in ("5272351424", "6530793575", "123456789", "Blue-01",
                      "Fwlai/Anon", "abcdef123", "service", "repo", "branch", "commit"):
            self.assertNotIn(hidden, dumped)
        self.assertEqual(record["details"]["nested"], {})

    def test_only_errors_selected_warnings_and_notable_events_reach_group(self):
        self.assertFalse(main.should_notify_group("message.forwarded_to_admin", "INFO"))
        self.assertFalse(main.should_notify_group("message.received", "INFO"))
        self.assertFalse(main.should_notify_group("message.rejected_blocked_user", "WARNING"))
        self.assertTrue(main.should_notify_group("message.album_late_part_rejected", "WARNING"))
        self.assertTrue(main.should_notify_group("message.delivery_rejected", "ERROR"))
        self.assertTrue(main.should_notify_group("system.startup_completed", "INFO"))
        self.assertTrue(main.should_notify_group("system.shutdown_started", "INFO"))

    async def test_routine_info_event_is_not_sent_to_group(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \\
             patch.object(main, "tg", new=AsyncMock()) as tg_mock, \\
             patch.object(main, "write_log_record"):
            await main.technical("message.forwarded_to_admin", "INFO", alias="Blue-01")
        tg_mock.assert_not_awaited()

    async def test_notable_event_uses_short_message_without_metadata(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \\
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \\
             patch.object(main, "write_log_record"):
            await main.technical(
                "system.startup_completed", "INFO",
                service="anon", repo="Fwlai/Anon", branch="main", commit="abcdef123"
            )
        tg_mock.assert_awaited_once_with("sendMessage", {
            "chat_id": 12345,
            "text": tg_mock.await_args.args[1]["text"],
            "disable_notification": True,
        })
        text = tg_mock.await_args.args[1]["text"]
        self.assertIn("system.startup_completed", text)
        self.assertNotIn("Fwlai/Anon", text)
        self.assertNotIn("Render", text)
        self.assertNotIn("commit", text)
        self.assertNotIn("json", text.lower())
        self.assertLess(len(text), 400)

    async def test_error_summary_is_short_and_privacy_filtered(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \\
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \\
             patch.object(main, "write_log_record"):
            await main.technical(
                "message.delivery_failed", "ERROR",
                provider="telegram", operation="copyMessage", http_status=429,
                alias="Blue-01", sender_user_id=5272351424,
                source_message_id=123456789, error="user 5272351424 not found",
                huge_value="x" * 6000,
            )
        text = tg_mock.await_args.args[1]["text"]
        self.assertIn("message.delivery_failed", text)
        self.assertIn("telegram", text)
        self.assertIn("copyMessage", text)
        self.assertIn("429", text)
        for hidden in ("Blue-01", "5272351424", "123456789", "huge_value", "xxxx"):
            self.assertNotIn(hidden, text)
        self.assertLess(len(text), 900)

    async def test_event_heading_is_sanitized(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \\
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \\
             patch.object(main, "write_log_record"):
            await main.technical("bad`event\\n```json", "ERROR")
        text = tg_mock.await_args.args[1]["text"]
        self.assertNotIn("`", text)
        self.assertNotIn("```", text)
        self.assertNotIn("\\n", text)
        self.assertIn("bad_event____json", text)

    async def test_repeated_error_is_sent_once_per_cooldown(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \\
             patch.object(main, "tg", new=AsyncMock(return_value={"message_id": 1})) as tg_mock, \\
             patch.object(main, "write_log_record"):
            for _ in range(3):
                await main.technical("test.repeat", "ERROR")
        tg_mock.assert_awaited_once()

    async def test_group_summary_ignores_private_and_large_details(self):
        record = main.make_log_record(
            "test.summary", "ERROR", alias="Purple-00",
            message_id=123456789, huge="x" * 2000,
            provider="supabase", operation="GET", http_status=502,
        )
        text = main.group_log_message(record)
        self.assertIn("test.summary", text)
        self.assertIn("supabase", text)
        self.assertIn("GET", text)
        self.assertIn("502", text)
        for hidden in ("Purple-00", "123456789", "xxxx", "huge"):
            self.assertNotIn(hidden, text)
        self.assertLess(len(text), 900)

    async def test_missing_log_group_skips_telegram_delivery(self):
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=None)), \\
             patch.object(main, "tg", new=AsyncMock()) as tg_mock, \\
             patch.object(main, "write_log_record"):
            record = await main.technical("test.no_group", "ERROR")
        self.assertEqual(record["event"], "test.no_group")
        tg_mock.assert_not_awaited()

    async def test_telegram_delivery_failure_does_not_trigger_duplicate_fallback(self):
        failure = main.IntegrationError(
            "telegram", "sendMessage", transport_error="ReadTimeout"
        )
        with patch.object(main, "log_chat_id", new=AsyncMock(return_value=12345)), \\
             patch.object(main, "tg", new=AsyncMock(side_effect=failure)) as tg_mock, \\
             patch.object(main, "write_log_record") as write_log:
            await main.technical("test.delivery_failure", "ERROR")
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

class HealthCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_report_summarizes_state_without_user_ids(self):
        async def fake_tg(method, payload):
            return {"pending_update_count": 0}
        with patch.object(main, "db_get", new=AsyncMock(return_value=[])), \\
             patch.object(main, "tg", new=AsyncMock(side_effect=fake_tg)):
            report = await main.build_health_report()
        self.assertTrue(report.startswith("✅ Anon is healthy"))
        self.assertIn("Database: ✅", report)
        self.assertIn("Telegram webhook: ✅ 0 pending", report)
        self.assertIn("Uptime:", report)
        self.assertNotIn("Render", report)
        self.assertNotIn("Fwlai/Anon", report)

    async def test_health_in_admin_private_chat_is_consumed_without_reaction_or_relay(self):
        message = {"message_id": 700, "text": "/health"}
        with patch.object(main, "react", new=AsyncMock()) as react:
            handled = await main.admin_command(message)
        self.assertTrue(handled)
        react.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
