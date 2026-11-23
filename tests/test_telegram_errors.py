import unittest
from unittest.mock import AsyncMock, patch

import httpx

import main


class TelegramErrorClassificationTests(unittest.TestCase):
    def test_rate_limit_error_code_is_retryable_even_with_http_200(self):
        error = main.IntegrationError(
            "telegram", "copyMessage", error_code=429, http_status=200
        )
        self.assertTrue(main.telegram_rate_limited(error))
        self.assertFalse(main.telegram_permanent_rejection(error))

    def test_http_429_takes_precedence_over_conflicting_error_code(self):
        error = main.IntegrationError(
            "telegram", "copyMessage", error_code=400, http_status=429
        )
        self.assertTrue(main.telegram_rate_limited(error))
        self.assertFalse(main.telegram_permanent_rejection(error))

    def test_permanent_client_rejection_is_terminal(self):
        error = main.IntegrationError(
            "telegram", "copyMessage", error_code=403, http_status=200
        )
        self.assertFalse(main.telegram_rate_limited(error))
        self.assertTrue(main.telegram_permanent_rejection(error))

    def test_http_5xx_takes_precedence_over_conflicting_error_code(self):
        error = main.IntegrationError(
            "telegram", "copyMessage", error_code=400, http_status=500
        )
        self.assertFalse(main.telegram_rate_limited(error))
        self.assertFalse(main.telegram_permanent_rejection(error))

    def test_transport_failure_is_not_classified_as_permanent(self):
        error = main.IntegrationError(
            "telegram", "copyMessage", transport_error="ReadTimeout"
        )
        self.assertFalse(main.telegram_rate_limited(error))
        self.assertFalse(main.telegram_permanent_rejection(error))


    def test_retry_delay_prefers_telegram_retry_after(self):
        error = main.IntegrationError(
            "telegram", "copyMessages", error_code=429, http_status=429,
            retry_after_seconds=17,
        )
        self.assertEqual(main.telegram_retry_delay(error, 0), 17)

    def test_retry_delay_uses_backoff_when_retry_after_is_missing_or_invalid(self):
        missing = main.IntegrationError(
            "telegram", "copyMessages", error_code=429, http_status=429
        )
        invalid = main.IntegrationError(
            "telegram", "copyMessages", error_code=429, http_status=429,
            retry_after_seconds=True,
        )
        self.assertEqual(main.telegram_retry_delay(missing, 0), 2)
        self.assertEqual(main.telegram_retry_delay(invalid, 2), 6)


class TelegramRetryAfterResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_429_retry_after_is_preserved(self):
        response = httpx.Response(429, json={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 17",
            "parameters": {"retry_after": 17},
        })
        with patch.object(main.client, "post", new=AsyncMock(return_value=response)):
            with self.assertRaises(main.IntegrationError) as raised:
                await main.tg("copyMessages", {"chat_id": 123})

        self.assertTrue(main.telegram_rate_limited(raised.exception))
        self.assertEqual(raised.exception.details.get("retry_after_seconds"), 17)


class AlbumRetryAfterWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_album_worker_waits_for_server_retry_after(self):
        failure = main.IntegrationError(
            "telegram", "copyMessages", error_code=429, http_status=429,
            retry_after_seconds=13,
        )
        key = ("user_to_admin", 123, main.ADMIN_ID, "retry-after")
        factory = AsyncMock(side_effect=[failure, None])
        with (
            patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep,
            patch.object(main, "technical", new=AsyncMock()),
            patch.dict(main._album_tasks, {}),
        ):
            await main.run_album_worker(key, factory)

        sleep.assert_awaited_once_with(13)
        self.assertEqual(factory.await_count, 2)



if __name__ == "__main__":
    unittest.main()
