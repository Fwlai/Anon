import unittest
from unittest.mock import AsyncMock, patch

import main


class StartupFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_recovery_migration_prevents_webhook_activation(self):
        failure = main.IntegrationError(
            "supabase", "RPC anon_expire_stale_albums",
            http_status=404, error="function not found",
        )
        with (
            patch.object(main, "BOT_TOKEN", "test-token"),
            patch.object(main, "ADMIN_ID", 123456),
            patch.object(main, "WEBHOOK_SECRET", "test-webhook-secret"),
            patch.object(main, "SUPABASE_URL", "https://example.supabase.co"),
            patch.object(main, "SUPABASE_KEY", "test-service-role-key"),
            patch.object(main, "db_get", new=AsyncMock(return_value=[])),
            patch.object(main, "db_rpc", new=AsyncMock(side_effect=failure)),
            patch.object(main, "tg", new=AsyncMock()) as tg,
            patch.object(main, "technical", new=AsyncMock()) as technical,
            patch.object(main, "_album_recovery_task", None),
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing to serve webhooks"):
                await main.startup()

        tg.assert_not_awaited()
        technical.assert_any_await(
            "system.startup_failed", "ERROR",
            provider="supabase", operation="RPC anon_expire_stale_albums",
            exception_type="IntegrationError", http_status=404,
            error="function not found",
        )
        self.assertIsNone(main._album_recovery_task)

    async def test_incomplete_configuration_prevents_webhook_activation(self):
        with (
            patch.object(main, "BOT_TOKEN", ""),
            patch.object(main, "ADMIN_ID", 0),
            patch.object(main, "WEBHOOK_SECRET", ""),
            patch.object(main, "SUPABASE_URL", ""),
            patch.object(main, "SUPABASE_KEY", ""),
            patch.object(main, "tg", new=AsyncMock()) as tg,
            patch.object(main, "technical", new=AsyncMock()) as technical,
            patch.object(main, "_album_recovery_task", None),
        ):
            with self.assertRaisesRegex(RuntimeError, "configuration is incomplete"):
                await main.startup()

        tg.assert_not_awaited()
        calls = [call.args[0] for call in technical.await_args_list]
        self.assertIn("system.configuration_incomplete", calls)
        self.assertIsNone(main._album_recovery_task)


if __name__ == "__main__":
    unittest.main()
