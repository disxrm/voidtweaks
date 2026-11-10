import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import account_api
import web_checkout


PLANS = {
    "month": {"name": "1 месяц", "price": 99, "days": 30},
}


class CheckoutAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_get_user = account_api.get_user
        web_checkout.init(
            supabase=None,
            plans=PLANS,
            shop_id=None,
            secret_key=None,
            site_url="",
            allowed_origins=set(),
            generate_key=lambda: "AAAA-BBBB-CCCC-DDDD",
        )
        app = web.Application()
        web_checkout.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        account_api.get_user = self.original_get_user

    async def test_checkout_rejects_guest(self):
        async def no_user(request):
            return None

        account_api.get_user = no_user
        response = await self.client.post(
            "/api/checkout", json={"plan": "month"}
        )
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
