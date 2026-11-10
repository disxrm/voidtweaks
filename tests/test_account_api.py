import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import account_api


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.filters = []
        self.action = "select"
        self.payload = None

    def select(self, _columns="*"):
        self.action = "select"
        return self

    def update(self, payload):
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        matched = [
            row for row in rows
            if all(str(row.get(column)) == str(value) for column, value in self.filters)
        ]
        if self.action == "select":
            return FakeResult([dict(row) for row in matched])
        for row in matched:
            row.update(self.payload or {})
        return FakeResult([dict(row) for row in matched])


class FakeSupabase:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        return FakeQuery(self.tables, name)


def make_db():
    return {
        "licenses": [
            {
                "license_key": "AAAA-BBBB-CCCC-DDDD",
                "plan": "month",
                "expires_at": "2026-12-09T12:00:00+00:00",
                "is_active": True,
                "hwid": None,
                "created_at": "2026-11-09T12:00:00+00:00",
                "user_id": None,
            },
            {
                "license_key": "EEEE-FFFF-GGGG-HHHH",
                "plan": "forever",
                "expires_at": None,
                "is_active": True,
                "hwid": "HWID-OTHER",
                "created_at": "2025-01-01T10:00:00+00:00",
                "user_id": "other-user",
            },
        ],
        "web_orders": [
            {
                "token": "order-other",
                "plan": "forever",
                "payment_id": "pay-other",
                "status": "succeeded",
                "license_key": "EEEE-FFFF-GGGG-HHHH",
                "user_id": "other-user",
            },
            {
                "token": "order-free",
                "plan": "month",
                "payment_id": "pay-free",
                "status": "succeeded",
                "license_key": "AAAA-BBBB-CCCC-DDDD",
                "user_id": None,
            },
            {
                "token": "order-pending",
                "plan": "month",
                "payment_id": "pay-pending",
                "status": "pending",
                "license_key": None,
                "user_id": None,
            },
        ],
    }


class AccountApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def fake_get_user(request):
            if request.headers.get("Authorization") == "Bearer good":
                return {"id": "user-1", "email": "user@example.com"}
            return None

        self._original_get_user = account_api.get_user
        account_api.get_user = fake_get_user
        account_api._supabase = FakeSupabase()
        account_api._supabase.tables = make_db()

        app = web.Application()
        account_api.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        account_api.get_user = self._original_get_user
        await self.client.close()

    async def test_keys_requires_auth(self):
        response = await self.client.get("/api/account/keys")
        self.assertEqual(response.status, 401)

    async def test_keys_returns_only_own_data(self):
        response = await self.client.get(
            "/api/account/keys", headers={"Authorization": "Bearer good"}
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["email"], "user@example.com")
        self.assertFalse(data["is_admin"])
        self.assertEqual(data["licenses"], [])
        self.assertEqual(data["orders"], [])
        self.assertNotIn("token", data["orders"][0] if data["orders"] else {})

        account_api._supabase.tables["licenses"][0]["user_id"] = "user-1"
        account_api._supabase.tables["web_orders"][1]["user_id"] = "user-1"
        response = await self.client.get(
            "/api/account/keys", headers={"Authorization": "Bearer good"}
        )
        data = await response.json()
        self.assertEqual(len(data["licenses"]), 1)
        self.assertEqual(data["licenses"][0]["license_key"], "AAAA-BBBB-CCCC-DDDD")
        self.assertNotIn("user_id", data["licenses"][0])
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(data["orders"][0]["license_key"], "AAAA-BBBB-CCCC-DDDD")
        self.assertNotIn("token", data["orders"][0])
        self.assertNotIn("payment_id", data["orders"][0])

    async def test_keys_reports_admin_flag(self):
        async def fake_admin(request):
            return {"id": "user-1", "email": "vit9aso2@gmail.com"}

        account_api.get_user = fake_admin
        response = await self.client.get(
            "/api/account/keys", headers={"Authorization": "Bearer good"}
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["is_admin"])

    async def test_claim_order_links_license_and_is_idempotent(self):
        response = await self.client.post(
            "/api/account/claim",
            json={"token": "order-free"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertFalse(data["pending"])
        self.assertEqual(data["license"]["license_key"], "AAAA-BBBB-CCCC-DDDD")
        self.assertEqual(account_api._supabase.tables["licenses"][0]["user_id"], "user-1")
        self.assertEqual(account_api._supabase.tables["web_orders"][1]["user_id"], "user-1")

        response = await self.client.post(
            "/api/account/claim",
            json={"token": "order-free"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 200)

    async def test_claim_other_users_order_rejected(self):
        response = await self.client.post(
            "/api/account/claim",
            json={"token": "order-other"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(
            account_api._supabase.tables["licenses"][1]["user_id"], "other-user"
        )

    async def test_claim_pending_order_marks_pending(self):
        response = await self.client.post(
            "/api/account/claim",
            json={"token": "order-pending"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["pending"])
        self.assertIsNone(data["license"])
        self.assertEqual(account_api._supabase.tables["web_orders"][2]["user_id"], "user-1")

    async def test_claim_missing_order(self):
        response = await self.client.post(
            "/api/account/claim",
            json={"token": "nope"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 404)

    async def test_claim_key_links_order(self):
        response = await self.client.post(
            "/api/account/claim",
            json={"key": "aaaa-bbbb-cccc-dddd"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["license"]["license_key"], "AAAA-BBBB-CCCC-DDDD")
        self.assertEqual(account_api._supabase.tables["licenses"][0]["user_id"], "user-1")
        self.assertEqual(account_api._supabase.tables["web_orders"][1]["user_id"], "user-1")

    async def test_claim_other_users_key_rejected(self):
        response = await self.client.post(
            "/api/account/claim",
            json={"key": "EEEE-FFFF-GGGG-HHHH"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 409)

    async def test_claim_bad_key_format(self):
        response = await self.client.post(
            "/api/account/claim",
            json={"key": "bad"},
            headers={"Authorization": "Bearer good"},
        )
        self.assertEqual(response.status, 400)


class GetUserTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def user_handler(request):
            if (
                request.headers.get("Authorization") == "Bearer good"
                and request.headers.get("apikey") == "secret"
            ):
                return web.json_response({"id": "user-1", "email": "user@example.com"})
            return web.json_response({"error": "bad"}, status=401)

        app = web.Application()
        app.router.add_get("/auth/v1/user", user_handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self._saved = (
            account_api._supabase,
            account_api._supabase_url,
            account_api._supabase_key,
        )
        account_api.init(
            supabase=None,
            supabase_url=str(self.server.make_url("")).rstrip("/"),
            supabase_key="secret",
        )

    async def asyncTearDown(self):
        await self.server.close()
        (
            account_api._supabase,
            account_api._supabase_url,
            account_api._supabase_key,
        ) = self._saved

    async def test_valid_token(self):
        request = make_mocked_request(
            "GET", "/", headers={"Authorization": "Bearer good"}
        )
        user = await account_api.get_user(request)
        self.assertEqual(user["id"], "user-1")

    async def test_invalid_token(self):
        request = make_mocked_request(
            "GET", "/", headers={"Authorization": "Bearer bad"}
        )
        self.assertIsNone(await account_api.get_user(request))

    async def test_missing_token(self):
        request = make_mocked_request("GET", "/")
        self.assertIsNone(await account_api.get_user(request))


if __name__ == "__main__":
    unittest.main()
