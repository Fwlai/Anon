import unittest

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


if __name__ == "__main__":
    unittest.main()
