# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

"""Pure-Python tests for the Biteship rates client (no real HTTP)."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.delivery_cost_estimator.services.biteship_client import (
    BiteshipRatesClient,
)

CLIENT_REQUESTS = (
    "odoo.addons.delivery_cost_estimator."
    "services.biteship_client.requests"
)

DUMMY_DIMENSIONS = {
    "origin_area_id": "IDNOP1IDNCO1IDNDO1",
    "destination_area_id": "IDNOP2IDNCO2IDNDO2",
}

PRICING_OK = [
    {
        "courier_name": "JNE",
        "courier_code": "jne",
        "courier_service_name": "REG",
        "courier_service_code": "reg",
        "duration": "2-3 days",
        "shipment_duration_range": "2-3",
        "shipment_duration_unit": "days",
        "price": 18000,
    },
    {
        "courier_name": "J&T",
        "courier_code": "jnt",
        "courier_service_name": "EZ",
        "courier_service_code": "ez",
        "duration": "3-4 days",
        "price": 15000,
    },
]


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else json.dumps(json_data or {})

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class TestBiteshipRatesClient(TransactionCase):
    """The BiteshipRatesClient never raises and never leaks the API key."""

    def _provider(self, **kwargs):
        data = {
            "api_base_url": "https://api.biteship.com",
            "api_key": "biteship_test.KEY",
            "auth_type": "api_key_header",
            "auth_header_name": "Authorization",
        }
        data.update(kwargs)
        return SimpleNamespace(**data)

    def _client(self, **kwargs):
        return BiteshipRatesClient(self._provider(), **kwargs)

    def _items(self):
        return [
            {
                "name": "Barang",
                "description": "General item",
                "value": 100000,
                "quantity": 1,
                "weight": 1000,
                "height": 10,
                "length": 10,
                "width": 10,
            }
        ]

    # ---- GET /v1/rates/couriers: success ----
    def test_get_rates_success(self):
        client = self._client()
        payload = dict(DUMMY_DIMENSIONS)
        payload.update({"success": True, "pricing": PRICING_OK})
        with patch(
            CLIENT_REQUESTS + ".post",
            return_value=FakeResponse(200, payload),
        ) as m:
            result = client.get_rates(
                "IDNOP1IDNCO1IDNDO1",
                "IDNOP2IDNCO2IDNDO2",
                ["jne", "jnt"],
                self._items(),
            )
        m.assert_called_once_with(
            "https://api.biteship.com/v1/rates/couriers",
            headers={"Authorization": "biteship_test.KEY"},
            json={
                "origin_area_id": "IDNOP1IDNCO1IDNDO1",
                "destination_area_id": "IDNOP2IDNCO2IDNDO2",
                "couriers": "jne,jnt",
                "items": self._items(),
            },
            timeout=10,
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(result["pricing"]), 2)
        self.assertEqual(result["pricing"][1]["courier_service_code"], "ez")

    def test_get_rates_missing_area_id(self):
        client = self._client()
        with patch(CLIENT_REQUESTS + ".post") as m:
            result = client.get_rates("", "DEST", ["jne"], self._items())
        m.assert_not_called()
        self.assertEqual(result["error_code"], "invalid_arguments")

    def test_get_rates_missing_couriers(self):
        client = self._client()
        with patch(CLIENT_REQUESTS + ".post") as m:
            result = client.get_rates("ORIG", "DEST", [], self._items())
        m.assert_not_called()
        self.assertEqual(result["error_code"], "invalid_arguments")

    # ---- timeout then retry ----
    def test_get_rates_timeout_then_retry_success(self):
        calls = {"n": 0}

        def fake_post(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            payload = dict(DUMMY_DIMENSIONS)
            payload.update({"success": True, "pricing": PRICING_OK})
            return FakeResponse(200, payload)

        client = self._client(retry_delay=0)
        with patch(CLIENT_REQUESTS + ".Timeout", TimeoutError), patch(
            CLIENT_REQUESTS + ".post", side_effect=fake_post
        ):
            result = client.get_rates(
                DUMMY_DIMENSIONS["origin_area_id"],
                DUMMY_DIMENSIONS["destination_area_id"],
                ["jne"],
                self._items(),
            )
        self.assertEqual(calls["n"], 2)
        self.assertTrue(result["success"])

    def test_get_rates_timeout_persistent(self):
        client = self._client(retry_delay=0)
        with patch(CLIENT_REQUESTS + ".Timeout", TimeoutError), patch(
            CLIENT_REQUESTS + ".post",
            side_effect=TimeoutError("timed out"),
        ):
            result = client.get_rates("ORIG", "DEST", ["jne"], self._items())
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network_error")
        self.assertIn("Biteship", result["error"])

    # ---- 5xx ----
    def test_get_rates_500_then_retry_success(self):
        calls = {"n": 0}

        def fake_post(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(500, {"success": False})
            payload = dict(DUMMY_DIMENSIONS)
            payload.update({"success": True, "pricing": PRICING_OK})
            return FakeResponse(200, payload)

        client = self._client(retry_delay=0)
        with patch(CLIENT_REQUESTS + ".post", side_effect=fake_post):
            result = client.get_rates(
                DUMMY_DIMENSIONS["origin_area_id"],
                DUMMY_DIMENSIONS["destination_area_id"],
                ["jne"],
                self._items(),
            )
        self.assertEqual(calls["n"], 2)
        self.assertTrue(result["success"])

    def test_get_rates_500_persistent_no_raise(self):
        client = self._client(retry_delay=0)
        with patch(
            CLIENT_REQUESTS + ".post",
            return_value=FakeResponse(500, {"success": False}),
        ):
            result = client.get_rates("ORIG", "DEST", ["jne"], self._items())
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "http_5xx")

    # ---- 4xx ----
    def test_get_rates_401_auth_error_no_key_leak(self):
        client = self._client()
        with patch(
            CLIENT_REQUESTS + ".post",
            return_value=FakeResponse(401, text="unauthorized"),
        ):
            result = client.get_rates("ORIG", "DEST", ["jne"], self._items())
        self.assertEqual(result["error_code"], "auth_error")
        self.assertEqual(result["pricing"], [])
        self.assertNotIn("KEY", result["error"])
        self.assertNotIn("biteship_test", result["error"])

    def test_get_rates_400_keeps_body_error_no_key_leak(self):
        payload = {"success": False, "error": "Data is not found", "code": 40003003}
        client = self._client()
        with patch(
            CLIENT_REQUESTS + ".post",
            return_value=FakeResponse(400, payload),
        ):
            result = client.get_rates("ORIG", "DEST", ["jne"], self._items())
        self.assertEqual(result["error_code"], "http_error")
        self.assertIn("Data is not found", result["error"])
        self.assertIn("HTTP 400", result["error"])
        self.assertNotIn("KEY", result["error"])

    # ---- success=false ----
    def test_get_rates_success_false(self):
        client = self._client()
        with patch(
            CLIENT_REQUESTS + ".post",
            return_value=FakeResponse(
                200, {"success": False, "message": "origin area not found"}
            ),
        ):
            result = client.get_rates("ORIG", "DEST", ["jne"], self._items())
        self.assertEqual(result["error_code"], "api_error")
        self.assertIn("origin area not found", result["error"])

    # ---- GET /v1/maps/areas ----
    def test_search_areas_success(self):
        client = self._client()
        areas = [
            {"id": "IDNxxx", "name": "Jakarta", "postal_code": "12345"},
        ]
        with patch(
            CLIENT_REQUESTS + ".get",
            return_value=FakeResponse(200, {"areas": areas}),
        ) as m:
            result = client.search_areas("12345", postal_code="12345")
        m.assert_called_once_with(
            "https://api.biteship.com/v1/maps/areas",
            headers={"Authorization": "biteship_test.KEY"},
            params={"countries": "ID", "input": "12345", "limit": 5},
            timeout=10,
        )
        self.assertEqual(result, areas)

    def test_search_areas_network_error(self):
        client = self._client()
        with patch(
            CLIENT_REQUESTS + ".get",
            side_effect=TimeoutError("timed out"),
        ), patch(CLIENT_REQUESTS + ".Timeout", TimeoutError):
            result = client.search_areas("Jakarta")
        self.assertEqual(result, [])

    def test_search_areas_empty_query(self):
        client = self._client()
        with patch(CLIENT_REQUESTS + ".get") as m:
            result = client.search_areas("")
        m.assert_not_called()
        self.assertEqual(result, [])

    # ---- reuse of parent get_couriers (same auth plumbing) ----
    def test_get_couriers_reused(self):
        client = self._client()
        couriers = [
            {"code": "jne", "name": "JNE Express"},
            {"code": "jnt", "name": "J&T Express"},
        ]
        with patch(
            CLIENT_REQUESTS + ".get",
            return_value=FakeResponse(200, {"couriers": couriers}),
        ) as m:
            result = client.get_couriers()
        m.assert_called_once_with(
            "https://api.biteship.com/v1/couriers",
            headers={"Authorization": "biteship_test.KEY"},
            timeout=10,
        )
        self.assertEqual(result, couriers)