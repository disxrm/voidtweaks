import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import account_api
import admin_api


PLANS = {
    "month": {"name": "1 месяц", "price": 99, "days": 30},
    "half": {"name": "6 месяцев", "price": 299, "days": 180},
    "forever": {"name": "Навсегда", "price": 499, "days": 36500},
}


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}
        self.patch = None

    def select(self, *args, **kwargs):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def execute(self):
        matched = [
            row for row in self.rows
            if all(row.get(key) == value for key, value in self.filters.items())
        ]
        if self.patch is not None:
            for row in matched:
                row.update(self.patch)
            return FakeResult([])
        return FakeResult([dict(row) for row in matched])


class FakeSupabase:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeQuery(self.tables.setdefault(name, []))


class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_get_user = account_api.get_user
        account_api.init(
            supabase=None,
            supabase_url="https://example.supabase.co",
            supabase_key="service-key",
            admin_emails=["vit9aso2@gmail.com"],
        )
        self.db = FakeSupabase({
            "licenses": [
                {
                    "license_key": "AAAA-BBBB-CCCC-DDDD",
                    "plan": "month",
                    "expires_at": "2030-01-01T00:00:00+00:00",
                    "is_active": True,
                    "hwid": "HWID-1",
                    "created_at": "2026-11-10T03:00:00+00:00",
                    "user_id": "u1",
                },
                {
                    "license_key": "EEEE-FFFF-GGGG-HHHH",
                    "plan": "forever",
                    "expires_at": "2126-01-01T00:00:00+00:00",
                    "is_active": False,
                    "hwid": None,
                    "created_at": "2026-11-09T03:00:00+00:00",
                    "user_id": None,
                },
            ],
            "web_orders": [
                {
                    "token": "token-1",
                    "plan": "month",
                    "status": "succeeded",
                    "license_key": "AAAA-BBBB-CCCC-DDDD",
                    "created_at": "2026-11-10T03:00:00+00:00",
                    "user_id": "u1",
                },
                {
                    "token": "token-2",
                    "plan": "half",
                    "status": "pending",
                    "license_key": None,
                    "created_at": "2026-11-10T04:00:00+00:00",
                    "user_id": "u1",
                },
            ],
        })
        admin_api.init(
            supabase=self.db,
            supabase_url="https://example.supabase.co",
            supabase_key="service-key",
            plans=PLANS,
        )

        async def users():
            return [
                {
                    "id": "u1",
                    "email": "vit9aso2@gmail.com",
                    "created_at": "2026-11-01T00:00:00+00:00",
                    "last_sign_in_at": "2026-11-10T03:00:00+00:00",
                }
            ]

        self.original_fetch_users = admin_api._fetch_auth_users
        admin_api._fetch_auth_users = users

        app = web.Application()
        admin_api.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        account_api.get_user = self.original_get_user
        admin_api._fetch_auth_users = self.original_fetch_users

    async def test_requires_auth(self):
        async def no_user(request):
            return None

        account_api.get_user = no_user
        response = await self.client.get("/api/admin/data")
        self.assertEqual(response.status, 401)

    async def test_rejects_non_admin(self):
        async def other_user(request):
            return {"id": "u2", "email": "other@example.com"}

        account_api.get_user = other_user
        response = await self.client.get("/api/admin/data")
        self.assertEqual(response.status, 403)

    async def test_returns_dashboard_data(self):
        async def admin_user(request):
            return {"id": "u1", "email": "vit9aso2@gmail.com"}

        account_api.get_user = admin_user
        response = await self.client.get("/api/admin/data")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stats"]["users"], 1)
        self.assertEqual(data["stats"]["licenses"], 2)
        self.assertEqual(data["stats"]["active_licenses"], 1)
        self.assertEqual(data["stats"]["orders"], 2)
        self.assertEqual(data["stats"]["pending_orders"], 1)
        self.assertEqual(data["stats"]["revenue"], 99)
        self.assertEqual(data["licenses"][0]["email"], "vit9aso2@gmail.com")

    async def test_toggle_license(self):
        async def admin_user(request):
            return {"id": "u1", "email": "vit9aso2@gmail.com"}

        account_api.get_user = admin_user
        response = await self.client.post(
            "/api/admin/license",
            json={"license_key": "AAAA-BBBB-CCCC-DDDD", "action": "toggle"},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertFalse(data["license"]["is_active"])
        self.assertFalse(self.db.tables["licenses"][0]["is_active"])


if __name__ == "__main__":
    unittest.main()
