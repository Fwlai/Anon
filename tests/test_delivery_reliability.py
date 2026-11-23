import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import main


class ConfirmedDeliveryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_message_is_not_copied_again_when_mapping_and_session_writes_fail(self):
        recent = datetime.now(timezone.utc).isoformat()
        failure = main.IntegrationError("supabase", "POST", http_status=503)

        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"last_message_at": recent}
        ])), patch.object(main, "tg", new=AsyncMock(return_value={
            "message_id": 901
        })) as tg, patch.object(main, "map_admin_message", new=AsyncMock(
            side_effect=failure
        )), patch.object(main, "db_post", new=AsyncMock(
            side_effect=failure
        )), patch.object(main, "technical", new=AsyncMock()) as technical:
            await main.relay_live({
                "message_id": 101,
                "from": {"id": 123},
                "text": "delivered once",
            }, {"alias": "Blue-01"})

        tg.assert_awaited_once_with("copyMessage", {
            "chat_id": main.ADMIN_ID,
            "from_chat_id": 123,
            "message_id": 101,
        })
        self.assertGreaterEqual(technical.await_count, 2)

    async def test_admin_message_is_not_reported_as_failed_after_copy_if_mapping_fails(self):
        with patch.object(main, "tg", new=AsyncMock(return_value={
            "message_id": 902
        })) as tg, patch.object(main, "map_outbound", new=AsyncMock(
            side_effect=main.IntegrationError("supabase", "POST", http_status=503)
        )), patch.object(main, "get_user", new=AsyncMock(return_value={
            "alias": "Blue-01"
        })), patch.object(main, "technical", new=AsyncMock()) as technical:
            await main.send_to_user({
                "message_id": 201,
                "audio": {"file_id": "audio"},
            }, 123)

        tg.assert_awaited_once_with("copyMessage", {
            "chat_id": 123,
            "from_chat_id": main.ADMIN_ID,
            "message_id": 201,
        })
        self.assertGreaterEqual(technical.await_count, 2)


class RetentionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_removes_old_processed_updates_and_album_rows(self):
        original = main._last_retention_cleanup
        self.addCleanup(setattr, main, "_last_retention_cleanup", original)
        main._last_retention_cleanup = 0.0

        with patch.object(main.time, "monotonic", return_value=100000.0), \
             patch.object(main, "db_delete", new=AsyncMock()) as db_delete, \
             patch.object(main, "technical", new=AsyncMock()):
            await main.cleanup_old_records_if_due()

        self.assertEqual(db_delete.await_count, 3)
        calls = db_delete.await_args_list
        self.assertEqual(calls[0].args[0], "anon_processed_updates")
        self.assertEqual(calls[1].args[0], "anon_album_buffer")
        self.assertEqual(calls[2].args[0], "anon_album_routes")
        for call in calls:
            self.assertIn("created_at", call.args[1])
            self.assertTrue(call.args[1]["created_at"].startswith("lt."))

    async def test_cleanup_does_not_run_again_before_interval(self):
        original = main._last_retention_cleanup
        self.addCleanup(setattr, main, "_last_retention_cleanup", original)
        main._last_retention_cleanup = 100.0

        with patch.object(main.time, "monotonic", return_value=101.0), \
             patch.object(main, "db_delete", new=AsyncMock()) as db_delete, \
             patch.object(main, "technical", new=AsyncMock()):
            await main.cleanup_old_records_if_due()

        db_delete.assert_not_awaited()


class AmbiguousSingleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_single_message_copy_is_not_replayed(self):
        recent = datetime.now(timezone.utc).isoformat()
        failure = main.IntegrationError(
            "telegram", "copyMessage", transport_error="ReadTimeout"
        )

        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"last_message_at": recent}
        ])), patch.object(main, "tg", new=AsyncMock(side_effect=failure)) as tg, \
             patch.object(main, "react_status", new=AsyncMock()) as react_status, \
             patch.object(main, "technical", new=AsyncMock()) as technical:
            await main.relay_live({
                "message_id": 106,
                "from": {"id": 123},
                "text": "outcome may be unknown",
            }, {"alias": "Blue-01"})

        tg.assert_awaited_once()
        react_status.assert_awaited_once_with(123, 106, False)
        technical.assert_awaited_once()



    async def test_ambiguous_header_send_is_not_replayed(self):
        failure = main.IntegrationError(
            "telegram", "sendRichMessage", transport_error="ReadTimeout"
        )
        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=[])),
            patch.object(main, "tg", new=AsyncMock(side_effect=failure)) as tg,
            patch.object(main, "react_status", new=AsyncMock()) as react_status,
            patch.object(main, "technical", new=AsyncMock()) as technical,
        ):
            await main.relay_live({
                "message_id": 107,
                "from": {"id": 123},
                "text": "header outcome may be unknown",
            }, {"alias": "Blue-01"})

        tg.assert_awaited_once()
        self.assertEqual(tg.await_args.args[0], "sendRichMessage")
        react_status.assert_awaited_once_with(123, 107, False)
        self.assertEqual(technical.await_args.args[0], "message.header_delivery_outcome_unknown")

    async def test_header_rate_limit_is_retryable(self):
        failure = main.IntegrationError(
            "telegram", "sendRichMessage", error_code=429, http_status=429
        )
        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=[])),
            patch.object(main, "tg", new=AsyncMock(side_effect=failure)),
        ):
            with self.assertRaises(main.IntegrationError):
                await main.relay_live({
                    "message_id": 108,
                    "from": {"id": 123},
                    "text": "retry header",
                }, {"alias": "Blue-01"})

class OpenInboxHeaderSuppressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_alias_does_not_get_re_reported_after_ten_minutes(self):
        old = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        user = {
            "telegram_user_id": 123,
            "alias": "Blue-01",
            "is_open": True,
        }
        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=[
                {"last_message_at": old}
            ])),
            patch.object(main, "tg", new=AsyncMock(return_value={
                "message_id": 901
            })) as tg,
            patch.object(main, "map_admin_message", new=AsyncMock()),
            patch.object(main, "db_post", new=AsyncMock()),
            patch.object(main, "technical", new=AsyncMock()),
        ):
            await main.relay_live({
                "message_id": 109,
                "from": {"id": 123},
                "text": "new message while inbox is open",
            }, user)

        self.assertEqual(tg.await_args.args[0], "copyMessage")
        self.assertEqual(tg.await_count, 1)

    async def test_closed_alias_still_gets_periodic_header(self):
        old = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        user = {
            "telegram_user_id": 123,
            "alias": "Blue-01",
            "is_open": False,
        }
        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=[
                {"last_message_at": old}
            ])),
            patch.object(main, "tg", new=AsyncMock(side_effect=[
                {"message_id": 902},
                {"message_id": 903},
            ])) as tg,
            patch.object(main, "map_admin_message", new=AsyncMock()),
            patch.object(main, "db_post", new=AsyncMock()),
            patch.object(main, "technical", new=AsyncMock()),
        ):
            await main.relay_live({
                "message_id": 110,
                "from": {"id": 123},
                "text": "new message while inbox is closed",
            }, user)

        self.assertEqual(tg.await_args_list[0].args[0], "sendRichMessage")
        self.assertEqual(tg.await_args_list[1].args[0], "copyMessage")


if __name__ == "__main__":
    unittest.main()
