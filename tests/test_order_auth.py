import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import account_api
import web_checkout


PLANS = {
    "month": {"name": "1 месяц", "price": 99, "days": 30},
}


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, table_name, db):
        self.table_name = table_name
        self.db = db
        self.filters = {}
        self.patch = None

    def select(self, *_args):
        return self

    def update(self, patch):
        self.patch = patch
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def execute(self):
        rows = self.db.setdefault(self.table_name, [])
        matched = [
            row
            for row in rows
            if all(str(row.get(column)) == str(value) for column, value in self.filters.items())
        ]
        if self.patch is not None:
            for row in matched:
                row.update(self.patch)
            return FakeResult([dict(row) for row in matched])
        return FakeResult([dict(row) for row in matched])


class FakeSupabase:
    def __init__(self, orders, licenses=None):
        self.tables = {
            "web_orders": orders,
            "licenses": licenses or [],
        }

    def table(self, name):
        return FakeQuery(name, self.tables)


class OrderAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_get_user = account_api.get_user
        self.original_issue = web_checkout.issue_web_license
        self.order = {
            "token": "owner-order",
            "plan": "month",
            "payment_id": "pay-1",
            "status": "pending",
            "license_key": None,
            "user_id": "user-1",
        }
        self.legacy_order = {
            "token": "legacy-order",
            "plan": "month",
            "payment_id": "pay-2",
            "status": "pending",
            "license_key": None,
            "user_id": None,
        }
        self.license = {
            "license_key": "AAAA-BBBB-CCCC-DDDD",
            "plan": "month",
            "expires_at": "2030-01-01T00:00:00+00:00",
            "is_active": True,
            "hwid": None,
            "user_id": None,
        }
        self.supabase = FakeSupabase([self.order, self.legacy_order], [self.license])
        web_checkout._checkout_hits.clear()
        web_checkout.init(
            supabase=self.supabase,
            plans=PLANS,
            shop_id=None,
            secret_key=None,
            site_url="",
            allowed_origins=set(),
            generate_key=lambda: "AAAA-BBBB-CCCC-DDDD",
        )

        async def fake_get_user(request):
            auth = request.headers.get("Authorization", "")
            if auth == "Bearer owner":
                return {"id": "user-1", "email": "owner@example.com"}
            if auth == "Bearer other":
                return {"id": "user-2", "email": "other@example.com"}
            return None

        async def fake_issue(token):
            return {
                "status": "succeeded",
                "key": "AAAA-BBBB-CCCC-DDDD",
                "plan": "month",
                "expires_at": "2030-01-01T00:00:00+00:00",
            }

        account_api.get_user = fake_get_user
        web_checkout.issue_web_license = fake_issue
        app = web.Application()
        web_checkout.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        account_api.get_user = self.original_get_user
        web_checkout.issue_web_license = self.original_issue
        web_checkout._checkout_hits.clear()

    async def test_order_requires_auth(self):
        response = await self.client.get("/api/order?token=owner-order")
        self.assertEqual(response.status, 401)

    async def test_order_rejects_another_user(self):
        response = await self.client.get(
            "/api/order?token=owner-order",
            headers={"Authorization": "Bearer other"},
        )
        self.assertEqual(response.status, 404)

    async def test_order_returns_key_to_owner(self):
        response = await self.client.get(
            "/api/order?token=owner-order",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["key"], "AAAA-BBBB-CCCC-DDDD")

    async def test_order_binds_legacy_order_to_first_user(self):
        response = await self.client.get(
            "/api/order?token=legacy-order",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.legacy_order["user_id"], "user-1")
        self.assertEqual(self.license["user_id"], "user-1")

    async def test_order_post_body_is_supported(self):
        response = await self.client.post(
            "/api/order",
            json={"token": "owner-order"},
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["key"], "AAAA-BBBB-CCCC-DDDD")


if __name__ == "__main__":
    unittest.main()
