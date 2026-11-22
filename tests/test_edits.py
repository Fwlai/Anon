import unittest
from unittest.mock import AsyncMock, patch

import main


class EditFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_text_edit_preserves_entities_and_link_preview_options(self):
        message = {
            "message_id": 101,
            "from": {"id": 123},
            "text": "formatted link",
            "entities": [{"type": "text_link", "offset": 0, "length": 9,
                          "url": "https://example.com"}],
            "link_preview_options": {"is_disabled": True},
        }

        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"admin_message_id": 901}
        ])), patch.object(main, "tg", new=AsyncMock()) as tg, \
             patch.object(main, "technical", new=AsyncMock()):
            await main.edit_incoming_copy(message)

        tg.assert_awaited_once_with("editMessageText", {
            "chat_id": main.ADMIN_ID,
            "message_id": 901,
            "text": "formatted link",
            "entities": message["entities"],
            "link_preview_options": message["link_preview_options"],
        })

    async def test_incoming_caption_edit_preserves_caption_entities(self):
        message = {
            "message_id": 102,
            "from": {"id": 123},
            "photo": [{}],
            "caption": "spoiler",
            "caption_entities": [{"type": "spoiler", "offset": 0, "length": 7}],
            "show_caption_above_media": True,
        }

        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"admin_message_id": 902}
        ])), patch.object(main, "tg", new=AsyncMock()) as tg, \
             patch.object(main, "technical", new=AsyncMock()):
            await main.edit_incoming_copy(message)

        tg.assert_awaited_once_with("editMessageCaption", {
            "chat_id": main.ADMIN_ID,
            "message_id": 902,
            "caption": "spoiler",
            "caption_entities": message["caption_entities"],
            "show_caption_above_media": True,
        })

    async def test_admin_text_edit_preserves_entities(self):
        message = {
            "message_id": 201,
            "text": "bold",
            "entities": [{"type": "bold", "offset": 0, "length": 4}],
        }

        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"sender_user_id": 123, "user_message_id": 801}
        ])), patch.object(main, "tg", new=AsyncMock()) as tg, \
             patch.object(main, "technical", new=AsyncMock()):
            self.assertTrue(await main.edit_admin_outbound_copy(message))

        tg.assert_awaited_once_with("editMessageText", {
            "chat_id": 123,
            "message_id": 801,
            "text": "bold",
            "entities": message["entities"],
        })

    async def test_admin_caption_edit_preserves_caption_entities(self):
        message = {
            "message_id": 202,
            "caption": "italic",
            "caption_entities": [{"type": "italic", "offset": 0, "length": 6}],
        }

        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"sender_user_id": 123, "user_message_id": 802}
        ])), patch.object(main, "tg", new=AsyncMock()) as tg, \
             patch.object(main, "technical", new=AsyncMock()):
            self.assertTrue(await main.edit_admin_outbound_copy(message))

        tg.assert_awaited_once_with("editMessageCaption", {
            "chat_id": 123,
            "message_id": 802,
            "caption": "italic",
            "caption_entities": message["caption_entities"],
        })



    async def test_incoming_edit_rate_limit_propagates_for_webhook_retry(self):
        message = {
            "message_id": 103,
            "from": {"id": 123},
            "text": "retry edit",
        }
        failure = main.IntegrationError(
            "telegram", "editMessageText", error_code=429, http_status=429
        )
        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=[
                {"admin_message_id": 903}
            ])),
            patch.object(main, "tg", new=AsyncMock(side_effect=failure)),
            patch.object(main, "react", new=AsyncMock()),
            patch.object(main, "get_user", new=AsyncMock(return_value={
                "alias": "Blue-01"
            })),
            patch.object(main, "technical", new=AsyncMock()),
        ):
            with self.assertRaises(main.IntegrationError):
                await main.edit_incoming_copy(message)

    async def test_admin_edit_rate_limit_propagates_for_webhook_retry(self):
        message = {
            "message_id": 203,
            "text": "retry edit",
        }
        failure = main.IntegrationError(
            "telegram", "editMessageText", error_code=429, http_status=429
        )
        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=[
                {"sender_user_id": 123, "user_message_id": 803}
            ])),
            patch.object(main, "tg", new=AsyncMock(side_effect=failure)),
            patch.object(main, "react", new=AsyncMock()),
            patch.object(main, "technical", new=AsyncMock()),
        ):
            with self.assertRaises(main.IntegrationError):
                await main.edit_admin_outbound_copy(message)


class EditWebhookRetryTests(unittest.IsolatedAsyncioTestCase):
    async def _post_edited_message(self, sender_id, handler_name):
        token = "11111111-1111-4111-8111-111111111111"
        failure = main.IntegrationError(
            "telegram", "editMessageText", error_code=429, http_status=429
        )
        with (
            patch.object(main, "WEBHOOK_SECRET", "test-secret"),
            patch.object(main, "mark_update", new=AsyncMock(return_value={
                "claimed": True, "state": "claimed", "lease_token": token
            })),
            patch.object(main, "unmark_update", new=AsyncMock()) as unmark,
            patch.object(main, handler_name, new=AsyncMock(side_effect=failure)),
            patch.object(main, "technical", new=AsyncMock()),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
                    json={
                        "update_id": 12346,
                        "edited_message": {
                            "message_id": 204,
                            "from": {"id": sender_id},
                            "chat": {"id": sender_id, "type": "private"},
                            "text": "edited",
                        },
                    },
                )
        self.assertEqual(response.status_code, 503)
        unmark.assert_awaited_once_with(12346, token)

    async def test_incoming_edit_rate_limit_returns_retryable_webhook_response(self):
        await self._post_edited_message(123, "edit_incoming_copy")

    async def test_admin_edit_rate_limit_returns_retryable_webhook_response(self):
        await self._post_edited_message(main.ADMIN_ID, "edit_admin_outbound_copy")


if __name__ == "__main__":
    unittest.main()
