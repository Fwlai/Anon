import unittest
from unittest.mock import AsyncMock, patch

import main


class AlbumRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_album_is_copied_as_one_group_and_mapped_atomically(self):
        message = {
            "message_id": 102,
            "from": {"id": 123},
            "media_group_id": "album-1",
            "photo": [{}],
        }
        buffered = [
            {"sender_user_id": 123, "direction": "user_to_admin",
             "message_id": 101, "reply_to_message_id": None},
            {"sender_user_id": 123, "direction": "user_to_admin",
             "message_id": 102, "reply_to_message_id": None},
        ]
        copied = [{"message_id": 901}, {"message_id": 902}]

        with patch.object(main, "claim_album_messages", new=AsyncMock(
            return_value=(123, "album-1", buffered)
        )), patch.object(main, "db_get", new=AsyncMock(return_value=[])), \
             patch.object(main, "tg", new=AsyncMock(side_effect=[
                 {"message_id": 900}, copied
             ])) as tg, \
             patch.object(main, "map_admin_message", new=AsyncMock()), \
             patch.object(main, "finish_album", new=AsyncMock()) as finish, \
             patch.object(main, "db_post", new=AsyncMock()), \
             patch.object(main, "technical", new=AsyncMock()):
            await main.relay_album_to_admin(message, {
                "telegram_user_id": 123, "alias": "Blue-01"
            })

        copy_call = next(call for call in tg.await_args_list
                         if call.args[0] == "copyMessages")
        self.assertEqual(copy_call.args[1], {
            "chat_id": main.ADMIN_ID,
            "from_chat_id": 123,
            "message_ids": [101, 102],
        })
        finish.assert_awaited_once_with(123, main.ADMIN_ID, "album-1", [
            {"source_message_id": 101, "destination_message_id": 901},
            {"source_message_id": 102, "destination_message_id": 902},
        ], "delivered")

    async def test_admin_album_is_copied_as_one_group_and_mapped_atomically(self):
        message = {
            "message_id": 202,
            "from": {"id": main.ADMIN_ID},
            "media_group_id": "album-2",
            "video": {"file_id": "video"},
        }
        buffered = [
            {"sender_user_id": 123, "direction": "admin_to_user",
             "message_id": 201, "reply_to_message_id": None},
            {"sender_user_id": 123, "direction": "admin_to_user",
             "message_id": 202, "reply_to_message_id": None},
        ]

        with patch.object(main, "claim_album_messages", new=AsyncMock(
            return_value=(main.ADMIN_ID, "album-2", buffered)
        )), patch.object(main, "tg", new=AsyncMock(return_value=[
            {"message_id": 801}, {"message_id": 802}
        ])) as tg, \
             patch.object(main, "finish_album", new=AsyncMock()) as finish, \
             patch.object(main, "get_user", new=AsyncMock(return_value={
                 "alias": "Blue-01"
             })), patch.object(main, "technical", new=AsyncMock()):
            await main.relay_album_to_user(message, 123)

        tg.assert_awaited_once_with("copyMessages", {
            "chat_id": 123,
            "from_chat_id": main.ADMIN_ID,
            "message_ids": [201, 202],
        })
        finish.assert_awaited_once_with(main.ADMIN_ID, 123, "album-2", [
            {"source_message_id": 201, "destination_message_id": 801},
            {"source_message_id": 202, "destination_message_id": 802},
        ], "delivered")

    async def test_album_copy_failure_resets_buffer_and_propagates_for_retry(self):
        message = {
            "message_id": 302,
            "from": {"id": 123},
            "media_group_id": "album-3",
        }
        buffered = [{
            "sender_user_id": 123, "direction": "user_to_admin",
            "message_id": 302, "reply_to_message_id": None,
        }]
        failure = main.IntegrationError("telegram", "copyMessages", http_status=400)

        with patch.object(main, "claim_album_messages", new=AsyncMock(
            return_value=(123, "album-3", buffered)
        )), patch.object(main, "db_get", new=AsyncMock(return_value=[])), \
             patch.object(main, "tg", new=AsyncMock(side_effect=[
                 {"message_id": 903}, failure
             ])), patch.object(main, "map_admin_message", new=AsyncMock()), \
             patch.object(main, "finish_album", new=AsyncMock()) as finish, \
             patch.object(main, "technical", new=AsyncMock()):
            with self.assertRaises(main.IntegrationError):
                await main.relay_album_to_admin(message, {
                    "telegram_user_id": 123, "alias": "Blue-01"
                })

        finish.assert_awaited_once_with(123, main.ADMIN_ID, "album-3", status="pending")


if __name__ == "__main__":
    unittest.main()
