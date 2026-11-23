import unittest
from unittest.mock import AsyncMock, patch

import main


class AlbumPreDeliveryFailureTests(unittest.IsolatedAsyncioTestCase):
    def album_message(self):
        return {
            "message_id": 901,
            "from": {"id": 123},
            "media_group_id": "pre-delivery",
        }

    def claimed_rows(self):
        return (123, "pre-delivery", [{
            "sender_user_id": 123,
            "direction": "user_to_admin",
            "message_id": 901,
            "reply_to_message_id": None,
        }])

    async def test_admin_session_lookup_failure_releases_claim_before_retry(self):
        with (
            patch.object(main, "claim_album_messages", new=AsyncMock(
                return_value=self.claimed_rows()
            )),
            patch.object(main, "db_get", new=AsyncMock(
                side_effect=RuntimeError("database unavailable")
            )),
            patch.object(main, "finish_album", new=AsyncMock()) as finish,
            patch.object(main, "technical", new=AsyncMock()),
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await main.deliver_album_to_admin(
                    self.album_message(), {"alias": "Blue-01"}
                )

        finish.assert_awaited_once_with(
            123, main.ADMIN_ID, "pre-delivery", status="pending"
        )
        tg.assert_not_awaited()

    async def test_recipient_lookup_failure_releases_claim_before_retry(self):
        with (
            patch.object(main, "claim_album_messages", new=AsyncMock(
                return_value=(
                    main.ADMIN_ID, "pre-delivery", [{
                        "sender_user_id": 123,
                        "direction": "admin_to_user",
                        "message_id": 901,
                        "reply_to_message_id": None,
                    }]
                )
            )),
            patch.object(main, "get_user", new=AsyncMock(
                side_effect=RuntimeError("database unavailable")
            )),
            patch.object(main, "finish_album", new=AsyncMock()) as finish,
            patch.object(main, "technical", new=AsyncMock()),
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await main.deliver_album_to_user(self.album_message(), 123)

        finish.assert_awaited_once_with(
            main.ADMIN_ID, 123, "pre-delivery", status="pending"
        )
        tg.assert_not_awaited()

    async def test_blocked_user_album_is_failed_without_sending(self):
        with (
            patch.object(main, "claim_album_messages", new=AsyncMock(
                return_value=self.claimed_rows()
            )),
            patch.object(main, "finish_album", new=AsyncMock()) as finish,
            patch.object(main, "react_status", new=AsyncMock()) as react_status,
            patch.object(main, "technical", new=AsyncMock()),
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            await main.deliver_album_to_admin(
                self.album_message(),
                {"alias": "Blue-01", "blocked_by_admin": True},
            )

        finish.assert_awaited_once_with(
            123, main.ADMIN_ID, "pre-delivery", status="failed"
        )
        react_status.assert_awaited_once_with(123, 901, False)
        tg.assert_not_awaited()

    async def test_blocked_recipient_album_is_failed_without_sending(self):
        with (
            patch.object(main, "claim_album_messages", new=AsyncMock(
                return_value=(
                    main.ADMIN_ID, "pre-delivery", [{
                        "sender_user_id": 123,
                        "direction": "admin_to_user",
                        "message_id": 901,
                        "reply_to_message_id": None,
                    }]
                )
            )),
            patch.object(main, "get_user", new=AsyncMock(return_value={
                "alias": "Blue-01", "blocked_by_admin": True
            })),
            patch.object(main, "finish_album", new=AsyncMock()) as finish,
            patch.object(main, "react", new=AsyncMock()) as react,
            patch.object(main, "technical", new=AsyncMock()),
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            await main.deliver_album_to_user(self.album_message(), 123)

        finish.assert_awaited_once_with(
            main.ADMIN_ID, 123, "pre-delivery", status="failed"
        )
        react.assert_awaited_once_with(main.ADMIN_ID, 901, "👎")
        tg.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
