import unittest
from unittest.mock import AsyncMock, patch

import main


class DurableAlbumRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_restarts_workers_for_old_pending_groups(self):
        rows = [
            {
                "source_chat_id": 123,
                "destination_chat_id": main.ADMIN_ID,
                "sender_user_id": 123,
                "direction": "user_to_admin",
                "media_group_id": "recovery-user",
                "message_id": 101,
            },
            {
                "source_chat_id": 123,
                "destination_chat_id": main.ADMIN_ID,
                "sender_user_id": 123,
                "direction": "user_to_admin",
                "media_group_id": "recovery-user",
                "message_id": 102,
            },
            {
                "source_chat_id": main.ADMIN_ID,
                "destination_chat_id": 456,
                "sender_user_id": 456,
                "direction": "admin_to_user",
                "media_group_id": "recovery-admin",
                "message_id": 201,
            },
        ]
        user = {"telegram_user_id": 123, "alias": "Blue-01"}
        with (
            patch.object(main, "db_rpc", new=AsyncMock(return_value=0)) as rpc,
            patch.object(main, "db_get", new=AsyncMock(return_value=rows)) as db_get,
            patch.object(main, "get_user", new=AsyncMock(return_value=user)),
            patch.object(main, "start_album_worker") as start,
        ):
            started = await main.recover_pending_albums_once()

        self.assertEqual(started, 2)
        rpc.assert_awaited_once_with("anon_expire_stale_albums", {
            "p_lease_seconds": main.ALBUM_PROCESSING_LEASE_SECONDS,
        })
        self.assertEqual(db_get.await_args.args[0], "anon_album_buffer")
        keys = [call.args[0] for call in start.call_args_list]
        self.assertIn(("user_to_admin", 123, main.ADMIN_ID, "recovery-user"), keys)
        self.assertIn(("admin_to_user", main.ADMIN_ID, 456, "recovery-admin"), keys)

    async def test_recovery_factory_uses_persisted_message_ids_after_restart(self):
        rows = [{
            "source_chat_id": 123,
            "destination_chat_id": main.ADMIN_ID,
            "sender_user_id": 123,
            "direction": "user_to_admin",
            "media_group_id": "recovery-factory",
            "message_id": 707,
        }]
        user = {"telegram_user_id": 123, "alias": "Blue-01"}
        with (
            patch.object(main, "db_rpc", new=AsyncMock(return_value=0)),
            patch.object(main, "db_get", new=AsyncMock(return_value=rows)),
            patch.object(main, "get_user", new=AsyncMock(return_value=user)),
            patch.object(main, "start_album_worker") as start,
            patch.object(main, "deliver_album_to_admin", new=AsyncMock()) as deliver,
        ):
            await main.recover_pending_albums_once()
            factory = start.call_args.args[1]
            await factory()

        deliver.assert_awaited_once_with({
            "message_id": 707,
            "media_group_id": "recovery-factory",
            "from": {"id": 123},
        }, user)

    async def test_stale_processing_groups_are_expired_before_scanning_pending(self):
        with (
            patch.object(main, "db_rpc", new=AsyncMock(return_value=2)) as rpc,
            patch.object(main, "db_get", new=AsyncMock(return_value=[])),
            patch.object(main, "technical", new=AsyncMock()) as technical,
        ):
            started = await main.recover_pending_albums_once()

        self.assertEqual(started, 0)
        technical.assert_awaited_once()
        self.assertEqual(
            technical.await_args.args[0],
            "message.album_stale_processing_marked_unknown",
        )
        self.assertEqual(rpc.await_args.args[0], "anon_expire_stale_albums")

    async def test_recovery_does_not_start_duplicate_in_process_worker(self):
        key = ("user_to_admin", 123, main.ADMIN_ID, "already-running")
        task = AsyncMock()
        task.done.return_value = False
        rows = [{
            "source_chat_id": 123,
            "destination_chat_id": main.ADMIN_ID,
            "sender_user_id": 123,
            "direction": "user_to_admin",
            "media_group_id": "already-running",
            "message_id": 808,
        }]
        user = {"telegram_user_id": 123, "alias": "Blue-01"}
        with (
            patch.object(main, "db_rpc", new=AsyncMock(return_value=0)),
            patch.object(main, "db_get", new=AsyncMock(return_value=rows)),
            patch.object(main, "get_user", new=AsyncMock(return_value=user)),
            patch.dict(main._album_tasks, {key: task}),
            patch.object(main, "start_album_worker") as start,
        ):
            started = await main.recover_pending_albums_once()

        self.assertEqual(started, 0)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
