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
             "message_id": 101, "reply_to_message_id": 555},
            {"sender_user_id": 123, "direction": "user_to_admin",
             "message_id": 102, "reply_to_message_id": 555},
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
            await main.deliver_album_to_admin(message, {
                "telegram_user_id": 123, "alias": "Blue-01"
            })

        copy_call = next(call for call in tg.await_args_list
                         if call.args[0] == "copyMessages")
        self.assertEqual(copy_call.args[1], {
            "chat_id": main.ADMIN_ID,
            "from_chat_id": 123,
            "message_ids": [101, 102],
            "reply_parameters": {"message_id": 555, "allow_sending_without_reply": True},
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
             "message_id": 201, "reply_to_message_id": 666},
            {"sender_user_id": 123, "direction": "admin_to_user",
             "message_id": 202, "reply_to_message_id": 666},
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
            await main.deliver_album_to_user(message, 123)

        tg.assert_awaited_once_with("copyMessages", {
            "chat_id": 123,
            "from_chat_id": main.ADMIN_ID,
            "message_ids": [201, 202],
            "reply_parameters": {"message_id": 666, "allow_sending_without_reply": True},
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
        failure = main.IntegrationError("telegram", "copyMessages", error_code=429, http_status=200)

        with patch.object(main, "claim_album_messages", new=AsyncMock(
            return_value=(123, "album-3", buffered)
        )), patch.object(main, "db_get", new=AsyncMock(return_value=[])), \
             patch.object(main, "tg", new=AsyncMock(side_effect=[
                 {"message_id": 903}, failure
             ])), patch.object(main, "map_admin_message", new=AsyncMock()), \
             patch.object(main, "finish_album", new=AsyncMock()) as finish, \
             patch.object(main, "technical", new=AsyncMock()):
            with self.assertRaises(main.IntegrationError):
                await main.deliver_album_to_admin(message, {
                    "telegram_user_id": 123, "alias": "Blue-01"
                })

        finish.assert_awaited_once_with(123, main.ADMIN_ID, "album-3", status="pending")


    async def test_late_album_part_is_rejected_after_group_claimed(self):
        message = {
            "message_id": 303,
            "from": {"id": 123},
            "media_group_id": "album-late",
            "photo": [{}],
        }

        with patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"status": "processing", "message_id": 301}
        ])), \
             patch.object(main, "db_post", new=AsyncMock()) as db_post, \
             patch.object(main, "db_patch", new=AsyncMock()) as db_patch, \
             patch.object(main, "react_status", new=AsyncMock()) as react_status, \
             patch.object(main, "technical", new=AsyncMock()):
            source, group_id, accepted = await main.buffer_album_message(
                message, 123, main.ADMIN_ID, "user_to_admin"
            )

        self.assertEqual((source, group_id, accepted), (123, "album-late", False))
        db_post.assert_not_awaited()
        db_patch.assert_not_awaited()
        react_status.assert_awaited_once_with(123, 303, False)


    async def test_ambiguous_album_copy_error_is_not_replayed(self):
        message = {
            "message_id": 304,
            "from": {"id": 123},
            "media_group_id": "album-unknown",
            "photo": [{}],
        }
        buffered = [{
            "sender_user_id": 123, "direction": "user_to_admin",
            "message_id": 304, "reply_to_message_id": None,
        }]
        failure = main.IntegrationError(
            "telegram", "copyMessages", transport_error="ReadTimeout"
        )
        recent = main.datetime.now(main.timezone.utc).isoformat()

        with patch.object(main, "claim_album_messages", new=AsyncMock(
            return_value=(123, "album-unknown", buffered)
        )), patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"last_message_at": recent}
        ])), patch.object(main, "tg", new=AsyncMock(side_effect=failure)), \
             patch.object(main, "finish_album", new=AsyncMock()) as finish, \
             patch.object(main, "react_status", new=AsyncMock()) as react_status, \
             patch.object(main, "technical", new=AsyncMock()):
            await main.deliver_album_to_admin(message, {
                "telegram_user_id": 123, "alias": "Blue-01"
            })

        finish.assert_awaited_once_with(
            123, main.ADMIN_ID, "album-unknown", status="delivered_unmapped"
        )
        react_status.assert_awaited_once_with(123, 304, False)

    async def test_permanent_album_copy_rejection_is_terminal(self):
        message = {
            "message_id": 305,
            "from": {"id": 123},
            "media_group_id": "album-rejected",
            "photo": [{}],
        }
        buffered = [{
            "sender_user_id": 123, "direction": "user_to_admin",
            "message_id": 305, "reply_to_message_id": None,
        }]
        failure = main.IntegrationError(
            "telegram", "copyMessages", error_code=400, http_status=200
        )
        recent = main.datetime.now(main.timezone.utc).isoformat()

        with patch.object(main, "claim_album_messages", new=AsyncMock(
            return_value=(123, "album-rejected", buffered)
        )), patch.object(main, "db_get", new=AsyncMock(return_value=[
            {"last_message_at": recent}
        ])), patch.object(main, "tg", new=AsyncMock(side_effect=failure)), \
             patch.object(main, "finish_album", new=AsyncMock()) as finish, \
             patch.object(main, "react_status", new=AsyncMock()), \
             patch.object(main, "technical", new=AsyncMock()):
            await main.deliver_album_to_admin(message, {
                "telegram_user_id": 123, "alias": "Blue-01"
            })

        finish.assert_awaited_once_with(
            123, main.ADMIN_ID, "album-rejected", status="failed"
        )


    async def test_album_rate_limit_retry_does_not_duplicate_header(self):
        message = {
            "message_id": 306,
            "from": {"id": 123},
            "media_group_id": "album-header-retry",
            "photo": [{}],
        }
        buffered = [{
            "sender_user_id": 123,
            "direction": "user_to_admin",
            "message_id": 306,
            "reply_to_message_id": None,
        }]
        recent = main.datetime.now(main.timezone.utc).isoformat()
        rate_limit = main.IntegrationError(
            "telegram", "copyMessages", error_code=429, http_status=200
        )

        with (
            patch.object(main, "claim_album_messages", new=AsyncMock(
                return_value=(123, "album-header-retry", buffered)
            )),
            patch.object(main, "db_get", new=AsyncMock(side_effect=[
                [], [{"last_message_at": recent}]
            ])),
            patch.object(main, "tg", new=AsyncMock(side_effect=[
                {"message_id": 900},
                rate_limit,
                [{"message_id": 901}],
            ])) as tg,
            patch.object(main, "map_admin_message", new=AsyncMock()) as map_header,
            patch.object(main, "db_post", new=AsyncMock()) as db_post,
            patch.object(main, "finish_album", new=AsyncMock()) as finish,
            patch.object(main, "technical", new=AsyncMock()),
        ):
            with self.assertRaises(main.IntegrationError):
                await main.deliver_album_to_admin(message, {
                    "telegram_user_id": 123, "alias": "Blue-01"
                })

            await main.deliver_album_to_admin(message, {
                "telegram_user_id": 123, "alias": "Blue-01"
            })

        header_calls = [
            call for call in tg.await_args_list
            if call.args[0] == "sendMessage"
        ]
        album_calls = [
            call for call in tg.await_args_list
            if call.args[0] == "copyMessages"
        ]
        self.assertEqual(len(header_calls), 1)
        self.assertEqual(len(album_calls), 2)
        map_header.assert_awaited_once_with(900, 123)
        self.assertEqual(db_post.await_count, 2)
        self.assertEqual(finish.await_count, 2)



    async def test_album_header_rate_limit_releases_claim_for_retry(self):
        message = {
            "message_id": 307,
            "from": {"id": 123},
            "media_group_id": "album-header-rate-limit",
            "photo": [{}],
        }
        buffered = [{
            "sender_user_id": 123,
            "direction": "user_to_admin",
            "message_id": 307,
            "reply_to_message_id": None,
        }]
        failure = main.IntegrationError(
            "telegram", "sendMessage", error_code=429, http_status=429
        )

        with (
            patch.object(main, "claim_album_messages", new=AsyncMock(
                return_value=(123, "album-header-rate-limit", buffered)
            )),
            patch.object(main, "db_get", new=AsyncMock(return_value=[])),
            patch.object(main, "tg", new=AsyncMock(side_effect=failure)),
            patch.object(main, "finish_album", new=AsyncMock()) as finish,
            patch.object(main, "technical", new=AsyncMock()),
        ):
            with self.assertRaises(main.IntegrationError):
                await main.deliver_album_to_admin(message, {
                    "telegram_user_id": 123, "alias": "Blue-01"
                })

        finish.assert_awaited_once_with(
            123, main.ADMIN_ID, "album-header-rate-limit", status="pending"
        )

    async def test_ambiguous_album_header_marks_group_terminal_without_replay(self):
        message = {
            "message_id": 308,
            "from": {"id": 123},
            "media_group_id": "album-header-unknown",
            "photo": [{}],
        }
        buffered = [{
            "sender_user_id": 123,
            "direction": "user_to_admin",
            "message_id": 308,
            "reply_to_message_id": None,
        }]
        failure = main.IntegrationError(
            "telegram", "sendMessage", transport_error="ReadTimeout"
        )

        with (
            patch.object(main, "claim_album_messages", new=AsyncMock(
                return_value=(123, "album-header-unknown", buffered)
            )),
            patch.object(main, "db_get", new=AsyncMock(return_value=[])),
            patch.object(main, "tg", new=AsyncMock(side_effect=failure)),
            patch.object(main, "finish_album", new=AsyncMock()) as finish,
            patch.object(main, "react_status", new=AsyncMock()) as react_status,
            patch.object(main, "technical", new=AsyncMock()) as technical,
        ):
            await main.deliver_album_to_admin(message, {
                "telegram_user_id": 123, "alias": "Blue-01"
            })

        finish.assert_awaited_once_with(
            123, main.ADMIN_ID, "album-header-unknown", status="delivered_unmapped"
        )
        react_status.assert_awaited_once_with(123, 308, False)
        self.assertTrue(any(
            call.args[0] == "message.album_header_outcome_unknown"
            for call in technical.await_args_list
        ))

if __name__ == "__main__":
    unittest.main()
