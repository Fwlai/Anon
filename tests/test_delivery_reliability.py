import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import main


class ConfirmedDeliveryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_message_is_not_copied_again_when_mapping_and_session_writes_fail(self):
        recent = datetime.now(timezone.utc).isoformat()
        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"last_message_at": recent}
        ])), patch.object(main, "tg", new=AsyncMock(return_value={
            "message_id": 901
        })) as tg, patch.object(main, "map_admin_message", new=AsyncMock(
            side_effect=main.IntegrationError("supabase", "POST", http_status=503)
        )), patch.object(main, "db_post", new=AsyncMock(
            side_effect=main.IntegrationError("supabase", "POST", http_status=503)
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

        with patch.object(main.time, "monotonic", return_value=10000.0), \
             patch.object(main, "db_delete", new=AsyncMock()) as db_delete, \
             patch.object(main, "technical", new=AsyncMock()):
            await main.cleanup_old_records_if_due()

        self.assertEqual(db_delete.await_count, 2)
        calls = db_delete.await_args_list
        self.assertEqual(calls[0].args[0], "anon_processed_updates")
        self.assertEqual(calls[1].args[0], "anon_album_buffer")
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


if __name__ == "__main__":
    unittest.main()
