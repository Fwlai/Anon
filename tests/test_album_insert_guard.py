import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20261122_anon_album_insert_guard.sql"
)


class AlbumInsertGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()

    def test_guard_is_installed_before_each_buffer_insert(self):
        self.assertIn("create trigger anon_album_buffer_insert_guard", self.sql)
        self.assertIn("before insert on public.anon_album_buffer", self.sql)
        self.assertIn("execute function public.anon_guard_album_buffer_insert()", self.sql)

    def test_insert_guard_uses_the_same_advisory_lock_key_as_album_claim(self):
        self.assertIn("pg_advisory_xact_lock", self.sql)
        self.assertIn("new.source_chat_id::text || ':'", self.sql)
        self.assertIn("new.destination_chat_id::text || ':'", self.sql)
        self.assertIn("new.media_group_id", self.sql)

    def test_non_pending_album_parts_are_suppressed_without_creating_a_new_row(self):
        guard = self.sql.split("if exists (", 1)[1].split("end if;", 1)[0]
        self.assertIn("album.status <> 'pending'", guard)
        self.assertIn("return null;", guard)

    def test_pending_group_last_seen_is_updated_inside_the_guard_transaction(self):
        self.assertIn("set last_seen_at = greatest(last_seen_at, new.last_seen_at)", self.sql)
        self.assertIn("and status = 'pending';", self.sql)


class AlbumInsertRaceFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_fast_path_persists_album_part_and_refreshes_quiet_window(self):
        message = {
            "message_id": 303,
            "from": {"id": 123},
            "media_group_id": "album-race",
            "photo": [{}],
        }

        with (
            patch.object(main, "db_get", new=AsyncMock(return_value=[])) as db_get,
            patch.object(main, "db_post", new=AsyncMock(return_value=[])) as db_post,
            patch.object(main, "db_patch", new=AsyncMock(return_value=[])) as db_patch,
            patch.object(main, "technical", new=AsyncMock()),
        ):
            source, group_id, accepted = await main.buffer_album_message(
                message, 123, main.ADMIN_ID, "user_to_admin"
            )

        self.assertEqual((source, group_id, accepted), (123, "album-race", True))
        db_get.assert_awaited_once()
        db_post.assert_awaited_once()
        db_patch.assert_awaited_once()
        self.assertEqual(db_post.await_args.args[0], "anon_album_buffer")


if __name__ == "__main__":
    unittest.main()
