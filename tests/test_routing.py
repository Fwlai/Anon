import unittest
from unittest.mock import AsyncMock, patch

import main


class IncomingRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_user_message_is_relayed_without_an_active_alias(self):
        message = {
            "message_id": 101,
            "from": {"id": 123},
            "voice": {"file_id": "voice-file"},
        }
        user = {"telegram_user_id": 123, "alias": "Blue-01"}

        with patch.object(main, "rate_ok", new=AsyncMock(return_value=True)), \
             patch.object(main, "technical", new=AsyncMock()), \
             patch.object(main, "relay_live", new=AsyncMock()) as relay, \
             patch.object(main, "db_get", new=AsyncMock()) as db_get:
            await main.handle_user(message, user)

        relay.assert_awaited_once_with(message, user, None)
        db_get.assert_not_awaited()

    async def test_reply_context_is_used_when_it_maps_to_an_admin_message(self):
        message = {
            "message_id": 102,
            "from": {"id": 123},
            "text": "reply",
            "reply_to_message": {"message_id": 88},
        }
        user = {"telegram_user_id": 123, "alias": "Blue-01"}

        with patch.object(main, "rate_ok", new=AsyncMock(return_value=True)), \
             patch.object(main, "technical", new=AsyncMock()), \
             patch.object(main, "db_get", new=AsyncMock(return_value=[
                 {"admin_message_id": 700}
             ])), \
             patch.object(main, "relay_live", new=AsyncMock()) as relay:
            await main.handle_user(message, user)

        relay.assert_awaited_once_with(message, user, 700)

    async def test_unmapped_user_reply_is_still_delivered_as_a_new_message(self):
        message = {
            "message_id": 103,
            "from": {"id": 123},
            "text": "still deliver this",
            "reply_to_message": {"message_id": 9999},
        }
        user = {"telegram_user_id": 123, "alias": "Blue-01"}

        with patch.object(main, "rate_ok", new=AsyncMock(return_value=True)), \
             patch.object(main, "technical", new=AsyncMock()), \
             patch.object(main, "db_get", new=AsyncMock(return_value=[])), \
             patch.object(main, "relay_live", new=AsyncMock()) as relay:
            await main.handle_user(message, user)

        relay.assert_awaited_once_with(message, user, None)



    async def test_unknown_slash_message_from_user_is_relayed(self):
        message = {
            "message_id": 104,
            "from": {"id": 123},
            "text": "/hello",
        }
        user = {"telegram_user_id": 123, "alias": "Blue-01"}

        with patch.object(main, "rate_ok", new=AsyncMock(return_value=True)), \
             patch.object(main, "technical", new=AsyncMock()), \
             patch.object(main, "relay_live", new=AsyncMock()) as relay:
            await main.handle_user(message, user)

        relay.assert_awaited_once_with(message, user, None)


class AdminRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_targets_message_author_even_when_another_alias_is_selected(self):
        reply_message = {
            "message_id": 501,
            "from": {"id": main.ADMIN_ID},
            "text": "admin reply",
            "reply_to_message": {"message_id": 700},
        }
        mapping = [{
            "admin_message_id": 700,
            "sender_user_id": 123,
            "user_message_id": 101,
        }]

        with patch.object(main, "admin_state", new=AsyncMock(return_value={
            "active_user_id": 999
        })), \
             patch.object(main, "db_get", new=AsyncMock(return_value=mapping)), \
             patch.object(main, "get_user", new=AsyncMock(return_value={
                 "telegram_user_id": 123,
                 "alias": "Blue-01",
                 "blocked_by_admin": False,
             })), \
             patch.object(main, "send_to_user", new=AsyncMock()) as send, \
             patch.object(main, "react", new=AsyncMock()) as react:
            await main.route_admin(reply_message)

        send.assert_awaited_once_with(reply_message, 123, 101)
        react.assert_not_awaited()

    async def test_reply_to_admin_copy_targets_user_without_active_alias(self):
        reply_message = {
            "message_id": 502,
            "from": {"id": main.ADMIN_ID},
            "text": "another reply",
            "reply_to_message": {"message_id": 701},
        }
        outbound_mapping = [{
            "admin_message_id": 701,
            "sender_user_id": 123,
            "user_message_id": 202,
        }]

        with patch.object(main, "admin_state", new=AsyncMock(return_value={
            "active_user_id": None
        })), \
             patch.object(main, "db_get", new=AsyncMock(side_effect=[
                 [], outbound_mapping
             ])), \
             patch.object(main, "get_user", new=AsyncMock(return_value={
                 "telegram_user_id": 123,
                 "alias": "Blue-01",
                 "blocked_by_admin": False,
             })), \
             patch.object(main, "send_to_user", new=AsyncMock()) as send:
            await main.route_admin(reply_message)

        send.assert_awaited_once_with(reply_message, 123, 202)

    async def test_admin_message_without_reply_or_selected_alias_gets_dislike(self):
        message = {
            "message_id": 503,
            "from": {"id": main.ADMIN_ID},
            "text": "no recipient",
        }

        with patch.object(main, "admin_state", new=AsyncMock(return_value={
            "active_user_id": None
        })), \
             patch.object(main, "react", new=AsyncMock()) as react, \
             patch.object(main, "technical", new=AsyncMock()):
            await main.route_admin(message)

        react.assert_awaited_once_with(main.ADMIN_ID, 503, "👎")

    async def test_i_without_alias_is_rejected(self):
        message = {"message_id": 504, "text": "/i"}

        with patch.object(main, "react", new=AsyncMock()) as react:
            handled = await main.admin_command(message)

        self.assertTrue(handled)
        react.assert_awaited_once_with(main.ADMIN_ID, 504, "👎")

    async def test_open_alias_switches_exclusive_active_recipient(self):
        user = {
            "telegram_user_id": 123,
            "alias": "Blue-01",
            "blocked_by_admin": False,
        }
        with (
            patch.object(main, "get_alias", new=AsyncMock(return_value=user)),
            patch.object(main, "admin_state", new=AsyncMock(return_value={
                "active_user_id": 456
            })),
            patch.object(main, "set_active", new=AsyncMock()) as set_active,
            patch.object(main, "set_open", new=AsyncMock()) as set_open,
            patch.object(main, "technical", new=AsyncMock()),
        ):
            opened = await main.open_alias("Blue-01", 505)

        self.assertTrue(opened)
        set_active.assert_awaited_once_with(123)
        self.assertEqual(
            [call.args for call in set_open.await_args_list],
            [(456, False), (123, True)],
        )

    async def test_header_rich_message_has_inline_rich_button(self):
        rich = main.header_rich_message("Blue-01", 123, False)
        text = rich["blocks"][0]["text"]
        self.assertEqual(text[0:2], ["Alias: ", {"type": "code", "text": "Blue-01"}])
        self.assertEqual(text[3]["type"], "button")
        self.assertEqual(text[3]["button"], {
            "text": "open",
            "style": "danger",
            "callback_data": "anon:open:123",
        })

    async def test_none_inbox_rich_message_has_no_button(self):
        rich = main.header_rich_message("none")
        self.assertEqual(rich["blocks"][0]["text"], [
            "Alias: ", {"type": "code", "text": "none"}
        ])

    async def test_open_callback_is_admin_only(self):
        query = {
            "id": "callback-1",
            "from": {"id": 999},
            "message": {"message_id": 77, "chat": {"id": main.ADMIN_ID}},
            "data": "anon:open:123",
        }
        with (
            patch.object(main, "tg", new=AsyncMock()) as tg,
            patch.object(main, "open_alias", new=AsyncMock()) as open_alias,
        ):
            await main.handle_admin_callback(query)

        open_alias.assert_not_awaited()
        tg.assert_awaited_once_with("answerCallbackQuery", {
            "callback_query_id": "callback-1",
            "text": "Not authorized",
            "show_alert": True,
        })

    async def test_rich_button_opens_alias_and_edits_only_touched_report(self):
        user = {
            "telegram_user_id": 123,
            "alias": "Blue-01",
            "blocked_by_admin": False,
            "is_open": False,
        }
        query = {
            "id": "callback-2",
            "from": {"id": main.ADMIN_ID},
            "message": {"message_id": 77, "chat": {"id": main.ADMIN_ID}},
            "data": "anon:open:123",
        }
        with (
            patch.object(main, "get_user", new=AsyncMock(return_value=user)),
            patch.object(main, "admin_state", new=AsyncMock(return_value={
                "active_user_id": None
            })),
            patch.object(main, "open_alias", new=AsyncMock(return_value=True)) as open_alias,
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            await main.handle_admin_callback(query)

        open_alias.assert_awaited_once_with("Blue-01", 77)
        self.assertEqual(tg.await_args_list[0].args[0], "editMessageText")
        self.assertEqual(tg.await_args_list[0].args[1], {
            "chat_id": main.ADMIN_ID,
            "message_id": 77,
            "rich_message": main.header_rich_message("Blue-01", 123, True),
        })
        self.assertEqual(tg.await_args_list[1].args[0], "answerCallbackQuery")
        self.assertEqual(tg.await_args_list[1].args[1], {
            "callback_query_id": "callback-2",
            "text": "Chat opened",
        })

    async def test_opening_with_i_does_not_edit_any_old_report_button(self):
        user = {
            "telegram_user_id": 123,
            "alias": "Blue-01",
            "blocked_by_admin": False,
        }
        with (
            patch.object(main, "get_alias", new=AsyncMock(return_value=user)),
            patch.object(main, "admin_state", new=AsyncMock(return_value={
                "active_user_id": None
            })),
            patch.object(main, "set_active", new=AsyncMock()),
            patch.object(main, "set_open", new=AsyncMock()),
            patch.object(main, "technical", new=AsyncMock()),
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            await main.admin_command({"text": "/i Blue-01", "message_id": 91})

        tg.assert_not_awaited()

    async def test_close_command_closes_only_the_active_alias(self):
        message = {"message_id": 78, "text": "/c"}
        with (
            patch.object(main, "admin_state", new=AsyncMock(
                return_value={"active_user_id": 123}
            )),
            patch.object(main, "set_open", new=AsyncMock()) as set_open,
            patch.object(main, "set_active", new=AsyncMock()) as set_active,
            patch.object(main, "technical", new=AsyncMock()),
        ):
            handled = await main.admin_command(message)

        self.assertTrue(handled)
        set_open.assert_awaited_once_with(123, False)
        set_active.assert_awaited_once_with(None)

    async def test_rich_button_closes_alias_and_edits_only_touched_report(self):
        user = {
            "telegram_user_id": 123,
            "alias": "Blue-01",
            "blocked_by_admin": False,
            "is_open": True,
        }
        query = {
            "id": "callback-toggle-close",
            "from": {"id": main.ADMIN_ID},
            "message": {"message_id": 77, "chat": {"id": main.ADMIN_ID}},
            "data": "anon:open:123",
        }
        with (
            patch.object(main, "get_user", new=AsyncMock(return_value=user)),
            patch.object(main, "admin_state", new=AsyncMock(return_value={
                "active_user_id": 123
            })),
            patch.object(main, "set_active", new=AsyncMock()) as set_active,
            patch.object(main, "set_open", new=AsyncMock()) as set_open,
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            await main.handle_admin_callback(query)

        set_active.assert_awaited_once_with(None)
        set_open.assert_awaited_once_with(123, False)
        self.assertEqual(tg.await_args_list[0].args[0], "editMessageText")
        self.assertEqual(tg.await_args_list[0].args[1], {
            "chat_id": main.ADMIN_ID,
            "message_id": 77,
            "rich_message": main.header_rich_message("Blue-01", 123, False),
        })
        self.assertEqual(tg.await_args_list[1].args[0], "answerCallbackQuery")
        self.assertEqual(tg.await_args_list[1].args[1], {
            "callback_query_id": "callback-toggle-close",
            "text": "Chat closed",
        })

    async def test_w_reports_active_alias_with_close_button(self):
        user = {"telegram_user_id": 123, "alias": "Blue-01"}
        with (
            patch.object(main, "admin_state", new=AsyncMock(return_value={
                "active_user_id": 123
            })),
            patch.object(main, "get_user", new=AsyncMock(return_value=user)),
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            handled = await main.admin_command({"text": "/w"})

        self.assertTrue(handled)
        self.assertEqual(tg.await_args.args[0], "sendRichMessage")
        self.assertEqual(
            tg.await_args.args[1]["rich_message"],
            main.header_rich_message("Blue-01", 123, True),
        )

    async def test_w_reports_none_without_a_button(self):
        with (
            patch.object(main, "admin_state", new=AsyncMock(return_value={
                "active_user_id": None
            })),
            patch.object(main, "get_user", new=AsyncMock()) as get_user,
            patch.object(main, "tg", new=AsyncMock()) as tg,
        ):
            handled = await main.admin_command({"text": "/w"})

        self.assertTrue(handled)
        get_user.assert_not_awaited()
        self.assertEqual(tg.await_args.args[0], "sendRichMessage")
        self.assertEqual(
            tg.await_args.args[1]["rich_message"],
            main.header_rich_message("none"),
        )


    async def test_unknown_slash_message_uses_selected_alias(self):
        message = {
            "message_id": 506,
            "from": {"id": main.ADMIN_ID},
            "text": "/hello",
        }

        with patch.object(main, "admin_state", new=AsyncMock(return_value={
            "active_user_id": 123
        })), \
             patch.object(main, "get_user", new=AsyncMock(return_value={
                 "telegram_user_id": 123,
                 "alias": "Blue-01",
                 "blocked_by_admin": False,
             })), \
             patch.object(main, "send_to_user", new=AsyncMock()) as send:
            await main.route_admin(message)

        send.assert_awaited_once_with(message, 123)


class ContentCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_voice_uses_copy_message_without_downloading(self):
        message = {
            "message_id": 601,
            "from": {"id": 123},
            "voice": {"file_id": "voice-file"},
        }
        user = {"telegram_user_id": 123, "alias": "Blue-01"}

        with patch.object(main, "db_get", new=AsyncMock(return_value=[])), \
             patch.object(main, "tg", new=AsyncMock(side_effect=[
                 {"message_id": 900},
                 {"message_id": 901},
             ])) as tg, \
             patch.object(main, "map_admin_message", new=AsyncMock()), \
             patch.object(main, "db_post", new=AsyncMock()), \
             patch.object(main, "technical", new=AsyncMock()):
            await main.relay_live(message, user)

        self.assertEqual(tg.await_args_list[1].args[0], "copyMessage")
        self.assertEqual(tg.await_args_list[1].args[1], {
            "chat_id": main.ADMIN_ID,
            "from_chat_id": 123,
            "message_id": 601,
        })

    async def test_admin_media_uses_copy_message_for_selected_user(self):
        message = {
            "message_id": 602,
            "from": {"id": main.ADMIN_ID},
            "audio": {"file_id": "audio-file"},
        }

        with patch.object(main, "tg", new=AsyncMock(return_value={
            "message_id": 902
        })) as tg, \
             patch.object(main, "map_outbound", new=AsyncMock()) as map_outbound, \
             patch.object(main, "get_user", new=AsyncMock(return_value={
                 "alias": "Blue-01"
             })), \
             patch.object(main, "technical", new=AsyncMock()):
            await main.send_to_user(message, 123)

        tg.assert_awaited_once_with("copyMessage", {
            "chat_id": 123,
            "from_chat_id": main.ADMIN_ID,
            "message_id": 602,
        })
        map_outbound.assert_awaited_once_with(902, 123, 602)


    async def test_admin_album_keeps_recipient_selected_by_first_album_item(self):
        message = {
            "message_id": 507,
            "from": {"id": main.ADMIN_ID},
            "media_group_id": "admin-album",
            "photo": [{}],
        }

        with patch.object(main, "db_get", new=AsyncMock(return_value=[{
            "recipient_user_id": 123,
            "reply_to_message_id": 88,
        }])), patch.object(main, "get_user", new=AsyncMock(return_value={
            "telegram_user_id": 123,
            "alias": "Blue-01",
            "blocked_by_admin": False,
        })), patch.object(main, "send_to_user", new=AsyncMock()) as send, \
             patch.object(main, "admin_state", new=AsyncMock()) as state:
            await main.route_admin(message)

        send.assert_awaited_once_with(message, 123, 88)
        state.assert_not_awaited()


    async def test_admin_album_persists_reply_found_on_a_later_album_part(self):
        message = {
            "message_id": 510,
            "from": {"id": main.ADMIN_ID},
            "media_group_id": "admin-album-late-reply",
            "reply_to_message": {"message_id": 700},
            "photo": [{}],
        }
        route = [{
            "recipient_user_id": 123,
            "reply_to_message_id": None,
        }]

        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=route)),
            patch.object(
                main, "mapped_admin_reply_target_for_user",
                new=AsyncMock(return_value=203),
            ),
            patch.object(main, "db_patch", new=AsyncMock()) as db_patch,
            patch.object(main, "db_rpc", new=AsyncMock(return_value=[{
                "recipient_user_id": 123,
                "reply_to_message_id": 203,
            }])) as db_rpc,
            patch.object(main, "get_user", new=AsyncMock(return_value={
                "telegram_user_id": 123,
                "alias": "Blue-01",
                "blocked_by_admin": False,
            })),
            patch.object(main, "send_to_user", new=AsyncMock()) as send,
        ):
            await main.route_admin(message)

        db_patch.assert_awaited_once_with(
            "anon_album_routes",
            {
                "source_chat_id": f"eq.{main.ADMIN_ID}",
                "media_group_id": "eq.admin-album-late-reply",
                "recipient_user_id": "eq.123",
                "reply_to_message_id": "is.null",
            },
            {"reply_to_message_id": 203},
        )
        db_rpc.assert_awaited_once_with("anon_reserve_admin_album_route", {
            "p_source_chat_id": main.ADMIN_ID,
            "p_media_group_id": "admin-album-late-reply",
            "p_candidate_user_id": 123,
            "p_reply_to_message_id": 203,
        })
        send.assert_awaited_once_with(message, 123, 203)


    async def test_album_route_reservation_keeps_atomic_recipient(self):
        message = {
            "message_id": 508,
            "from": {"id": main.ADMIN_ID},
            "media_group_id": "admin-album-race",
            "photo": [{}],
        }

        with patch.object(main, "db_rpc", new=AsyncMock(return_value=[{
            "recipient_user_id": 123,
            "reply_to_message_id": 44,
        }])) as db_rpc, \
             patch.object(main, "relay_album_to_user", new=AsyncMock()) as relay:
            await main.send_to_user(message, 999, 22)

        db_rpc.assert_awaited_once_with("anon_reserve_admin_album_route", {
            "p_source_chat_id": main.ADMIN_ID,
            "p_media_group_id": "admin-album-race",
            "p_candidate_user_id": 999,
            "p_reply_to_message_id": 22,
        })
        relay.assert_awaited_once_with(message, 123, 44)



    async def test_admin_reply_rate_limit_is_retried(self):
        message = {
            "message_id": 509,
            "from": {"id": main.ADMIN_ID},
            "reply_to_message": {"message_id": 702},
        }
        with patch.object(main, "db_get", new=AsyncMock(return_value=[{
            "sender_user_id": 123,
            "user_message_id": 203,
        }])), patch.object(main, "get_user", new=AsyncMock(return_value={
            "alias": "Blue-01",
            "blocked_by_admin": False,
        })), patch.object(main, "send_to_user", new=AsyncMock(
            side_effect=main.IntegrationError(
                "telegram", "copyMessage", error_code=429, http_status=429
            )
        )), patch.object(main, "react", new=AsyncMock()), \
             patch.object(main, "technical", new=AsyncMock()):
            with self.assertRaises(main.IntegrationError):
                await main.route_admin(message)



    async def test_selected_alias_rate_limit_is_retried(self):
        message = {
            "message_id": 510,
            "from": {"id": main.ADMIN_ID},
            "text": "retry this delivery",
        }

        with (
            patch.object(main, "admin_state", new=AsyncMock(return_value={
                "active_user_id": 123
            })),
            patch.object(main, "get_user", new=AsyncMock(return_value={
                "telegram_user_id": 123,
                "alias": "Blue-01",
                "blocked_by_admin": False,
            })),
            patch.object(main, "send_to_user", new=AsyncMock(
                side_effect=main.IntegrationError(
                    "telegram", "copyMessage", error_code=429, http_status=200
                )
            )),
            patch.object(main, "react", new=AsyncMock()),
            patch.object(main, "technical", new=AsyncMock()),
        ):
            with self.assertRaises(main.IntegrationError):
                await main.route_admin(message)


if __name__ == "__main__":
    unittest.main()
