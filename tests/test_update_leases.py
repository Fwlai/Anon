import unittest
from unittest.mock import AsyncMock, patch

import main


class UpdateLeaseHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_mark_update_claims_through_database_rpc(self):
        rows = [{
            "claimed": True,
            "state": "claimed",
            "lease_token": "11111111-1111-4111-8111-111111111111",
        }]
        with patch.object(main, "db_rpc", new=AsyncMock(return_value=rows)) as db_rpc:
            claim = await main.mark_update(12345)

        self.assertEqual(claim, rows[0])
        db_rpc.assert_awaited_once_with("anon_claim_update", {
            "p_update_id": 12345,
            "p_lease_seconds": 300,
        })

    async def test_ack_update_completes_lease_before_acknowledging(self):
        token = "11111111-1111-4111-8111-111111111111"
        with patch.object(main, "db_rpc", new=AsyncMock(return_value=True)) as db_rpc:
            response = await main.ack_update(12345, token)

        self.assertEqual(response, {"ok": True})
        db_rpc.assert_awaited_once_with("anon_complete_update", {
            "p_update_id": 12345,
            "p_lease_token": token,
        })

    async def test_ack_without_update_id_does_not_call_database(self):
        with patch.object(main, "db_rpc", new=AsyncMock()) as db_rpc:
            response = await main.ack_update(None)

        self.assertEqual(response, {"ok": True})
        db_rpc.assert_not_awaited()

    async def test_ack_does_not_claim_success_when_lease_completion_fails(self):
        with patch.object(main, "db_rpc", new=AsyncMock(return_value=False)):
            with self.assertRaises(RuntimeError):
                await main.ack_update(
                    12345, "11111111-1111-4111-8111-111111111111"
                )


if __name__ == "__main__":
    unittest.main()
