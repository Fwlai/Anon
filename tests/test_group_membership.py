import unittest
from unittest.mock import AsyncMock, patch

import httpx

import main


class GroupMembershipGuardTests(unittest.IsolatedAsyncioTestCase):
    async def deliver_membership_update(self, chat_id):
        log_group_id = -1004323044570
        with (
            patch.object(main, "WEBHOOK_SECRET", "test-secret"),
            patch.object(main, "mark_update", new=AsyncMock(return_value={
                "claimed": True,
                "state": "processing",
                "lease_token": "11111111-1111-4111-8111-111111111111",
            })),
            patch.object(main, "cleanup_old_records_if_due", new=AsyncMock()),
            patch.object(main, "log_chat_id", new=AsyncMock(
                return_value=log_group_id
            )),
            patch.object(main, "tg", new=AsyncMock(return_value=True)) as tg,
            patch.object(main, "technical", new=AsyncMock()),
            patch.object(main, "ack_update", new=AsyncMock(
                return_value={"ok": True}
            )) as ack,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/telegram/webhook",
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": "test-secret"
                    },
                    json={
                        "update_id": 90001,
                        "my_chat_member": {
                            "chat": {
                                "id": chat_id,
                                "type": "supergroup",
                            },
                            "new_chat_member": {"status": "member"},
                        },
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        ack.assert_awaited_once()
        return tg

    async def test_registered_log_group_is_kept(self):
        tg = await self.deliver_membership_update(-1004323044570)

        tg.assert_not_awaited()

    async def test_unregistered_group_is_left(self):
        tg = await self.deliver_membership_update(-1009876543210)

        tg.assert_awaited_once_with(
            "leaveChat", {"chat_id": -1009876543210}
        )


if __name__ == "__main__":
    unittest.main()
