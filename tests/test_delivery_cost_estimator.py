# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

"""Model-level tests: items/weight, area caching, courier TTL, parsing & flags.

All Biteship HTTP calls are mocked at the client-class level (``search_areas``,
``get_couriers``) so nothing ever reaches the network during the suite.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

CLIENT_PATH = (
    "odoo.addons.delivery_cost_estimator."
    "models.stock_picking.BiteshipRatesClient"
)
SEARCH_AREAS = CLIENT_PATH + ".search_areas"
GET_COURIERS = CLIENT_PATH + ".get_couriers"

ORIGIN_AREA = "delivery_cost_estimator.origin_area_id"
ACTIVE_COURIERS = "delivery_cost_estimator.active_couriers"
COURIERS_UPDATED = "delivery_cost_estimator.active_couriers_updated"
DEFAULT_DIMS = "delivery_cost_estimator.default_item_dimensions"

RATES = CLIENT_PATH + ".get_rates"

PRICING_OK = [
    {
        "courier_name": "JNE",
        "courier_code": "jne",
        "courier_service_name": "REG",
        "courier_service_code": "reg",
        "duration": "2-3 days",
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


class TestDeliveryCostEstimatorHelpers(TransactionCase):
    """Items assembly, area resolution/caching and courier TTL caching."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.Provider = cls.env["delivery.tracking.provider"]
        cls.Icp = cls.env["ir.config_parameter"]
        cls.partner = cls.env["res.partner"].create(
            {
                "name": "Test Customer",
                "street": "Jl. Sudirman No. 1",
                "city": "Jakarta",
                "zip": "12345",
            }
        )
        cls.out_type = cls.env.ref("stock.picking_type_out")
        cls.provider = cls.Provider.create(
            {
                "name": "Biteship",
                "api_base_url": "https://api.biteship.com",
                "api_key": "biteship_test.KEY",
            }
        )
        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Product",
                "type": "consu",
                "weight": 2.0,
                "list_price": 50000,
            }
        )

    def _make_picking(self, with_move=True):
        picking = self.Picking.create(
            {
                "picking_type_id": self.out_type.id,
                "partner_id": self.partner.id,
            }
        )
        if with_move:
            self.env["stock.move"].create(
                {
                    "name": self.product.name,
                    "product_id": self.product.id,
                    "product_uom_qty": 3,
                    "product_uom": self.product.uom_id.id,
                    "picking_id": picking.id,
                    "location_id": picking.location_id.id,
                    "location_dest_id": picking.location_dest_id.id,
                }
            )
        return picking

    # ---- _build_delivery_items ----
    def test_build_items_weight_and_default_dims(self):
        self.Icp.set_param(DEFAULT_DIMS, "5x5x5")
        picking = self._make_picking()
        items = picking._build_delivery_items()
        self.assertEqual(len(items), 1)
        item = items[0]
        # 2 kg * 1000 = 2000 g per unit, qty 3
        self.assertEqual(item["weight"], 2000)
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["length"], 5)
        self.assertEqual(item["width"], 5)
        self.assertEqual(item["height"], 5)
        self.assertEqual(item["value"], 150000)

    def test_build_items_min_weight_guard(self):
        self.product.write({"weight": 0.0})
        self.Icp.set_param(DEFAULT_DIMS, "5x5x5")
        picking = self._make_picking()
        items = picking._build_delivery_items()
        self.assertTrue(items)
        total = sum(i["weight"] * i["quantity"] for i in items)
        self.assertGreaterEqual(total, 1)

    def test_build_items_no_moves(self):
        picking = self._make_picking(with_move=False)
        self.assertEqual(picking._build_delivery_items(), [])

    def test_default_dims_parsing_fallback(self):
        self.Icp.set_param(DEFAULT_DIMS, "garbage")
        self.assertEqual(
            self.Picking._dce_default_dimensions(), (10, 10, 10)
        )

    # ---- origin area resolution ----
    def test_origin_config_wins(self):
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        picking = self._make_picking()
        with patch(SEARCH_AREAS) as m:
            self.assertEqual(picking._resolve_origin_area_id(), "ORIG-CONFIG")
        m.assert_not_called()

    def test_origin_resolved_and_persisted(self):
        self.Icp.set_param(ORIGIN_AREA, "")
        picking = self._make_picking()
        with patch(
            SEARCH_AREAS,
            return_value=[{"id": "ORIG-X", "postal_code": "10410"}],
        ):
            area = picking._resolve_origin_area_id()
        self.assertEqual(area, "ORIG-X")
        self.assertEqual(self.Icp.get_param(ORIGIN_AREA), "ORIG-X")

    # ---- destination area caching on partner ----
    def test_destination_cached_on_partner(self):
        picking = self._make_picking()
        with patch(
            SEARCH_AREAS,
            return_value=[{"id": "DEST-1", "postal_code": "12345"}],
        ) as m:
            self.assertEqual(picking._get_delivery_destination_area_id(), "DEST-1")
        self.assertEqual(self.partner.biteship_area_id, "DEST-1")
        # second call reuses the partner cache, never hits the network
        with patch(SEARCH_AREAS) as m2:
            self.assertEqual(picking._get_delivery_destination_area_id(), "DEST-1")
        m2.assert_not_called()
        self.assertEqual(m.call_count, 1)

    def test_destination_unresolvable_returns_none(self):
        picking = self._make_picking()
        with patch(SEARCH_AREAS, return_value=[]):
            self.assertIsNone(picking._get_delivery_destination_area_id())
        self.assertFalse(self.partner.biteship_area_id)

    # ---- active couriers TTL caching ----
    def test_active_couriers_fresh_cache_no_fetch(self):
        self.Icp.set_param(ACTIVE_COURIERS, "jne,jnt")
        self.Icp.set_param(COURIERS_UPDATED, datetime.now().isoformat())
        picking = self._make_picking()
        with patch(GET_COURIERS) as m:
            result = picking._get_active_couriers()
        self.assertEqual(result, ["jne", "jnt"])
        m.assert_not_called()

    def test_active_couriers_expired_refetches_and_persists(self):
        self.Icp.set_param(ACTIVE_COURIERS, "jne")
        self.Icp.set_param(
            COURIERS_UPDATED,
            (datetime.now() - timedelta(days=2)).isoformat(),
        )
        picking = self._make_picking()
        with patch(
            GET_COURIERS,
            return_value=[
                {"courier_code": "lion", "courier_name": "Lion Parcel"}
            ],
        ) as m:
            result = picking._get_active_couriers()
        self.assertEqual(result, ["lion"])
        m.assert_called_once()
        self.assertEqual(self.Icp.get_param(ACTIVE_COURIERS), "lion")
        self.assertTrue(self.Icp.get_param(COURIERS_UPDATED))

    def test_active_couriers_code_field_fallback(self):
        """Legacy ``code`` field also accepted (defensive, not API's shape)."""
        picking = self._make_picking()
        with patch(
            GET_COURIERS,
            return_value=[{"code": "jne", "name": "JNE Express"}],
        ) as m:
            result = picking._get_active_couriers()
        self.assertEqual(result, ["jne"])
        m.assert_called_once()

    def test_settings_refresh_couriers_fills_config(self):
        """Settings button parses ``courier_code`` payload into the field."""
        settings = self.env["res.config.settings"].create({})
        fake = SimpleNamespace(
            get_couriers=lambda: [
                {"courier_code": "jne", "courier_name": "JNE Express"},
                {"courier_code": "jnt", "courier_name": "J&T Express"},
            ]
        )
        with patch(
            "odoo.addons.delivery_cost_estimator.models.stock_picking."
            "StockPicking._dce_client",
            return_value=fake,
        ) as m:
            result = settings.action_dce_refresh_couriers()
        m.assert_called_once()
        self.assertEqual(settings.dce_active_couriers, "jne,jnt")
        self.assertEqual(result["params"]["type"], "success")


class TestDeliveryCostEstimateLines(TransactionCase):
    """Parsing, price/duration flags and selection persistence."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.Line = cls.env["delivery.cost.estimate.line"]
        cls.out_type = cls.env.ref("stock.picking_type_out")
        cls.partner = cls.env["res.partner"].create({"name": "Test Customer"})
        cls.picking = cls.Picking.create(
            {
                "picking_type_id": cls.out_type.id,
                "partner_id": cls.partner.id,
            }
        )

    def _make_line(self, price, duration, code="jne", name="JNE", service="REG"):
        return self.Line.create(
            {
                "picking_id": self.picking.id,
                "courier_code": code,
                "courier_name": name,
                "service_code": service.lower(),
                "service_name": service,
                "price": price,
                "duration_text": duration,
            }
        )

    def _refresh(self, lines):
        self.env.flush_all()
        lines.invalidate_recordset()

    # ---- parsing ----
    def test_parse_pricing_builds_line_dicts(self):
        pricing = [
            {
                "courier_name": "JNE",
                "courier_code": "jne",
                "courier_service_name": "REG",
                "courier_service_code": "reg",
                "duration": "2-3 days",
                "price": 18000,
            },
        ]
        parsed = self.Picking._parse_pricing(pricing)
        self.assertEqual(parsed, [
            {
                "courier_code": "jne",
                "courier_name": "JNE",
                "service_code": "reg",
                "service_name": "REG",
                "price": 18000,
                "duration_text": "2-3 days",
            },
        ])

    def test_parse_pricing_empty_and_malformed(self):
        self.assertEqual(self.Picking._parse_pricing([]), [])
        self.assertEqual(
            self.Picking._parse_pricing([None, "jne", {"courier_code": "jnt"}]),
            [{"courier_code": "jnt", "courier_name": "",
              "service_code": "", "service_name": "", "price": 0,
              "duration_text": ""}],
        )

    # ---- flags ----
    def test_cheapest_and_fastest_labels(self):
        cheap = self._make_line(15000, "3-4 days", code="jnt", name="J&T")
        middle = self._make_line(18000, "2-3 days", code="jne", name="JNE")
        fast = self._make_line(20000, "1-2 days", code="sicepat", name="SiCepat")
        self._refresh(cheap | middle | fast)
        self.assertTrue(cheap.is_cheapest)
        self.assertFalse(cheap.is_fastest)
        self.assertTrue(fast.is_fastest)
        self.assertFalse(fast.is_cheapest)
        self.assertFalse(middle.is_cheapest or middle.is_fastest)

    def test_cheapest_tie_both_flagged(self):
        a = self._make_line(15000, "2-3 days", code="jne")
        b = self._make_line(15000, "2-3 days", code="jnt", name="J&T")
        c = self._make_line(20000, "2-3 days", code="sicepat", name="SiCepat")
        self._refresh(a | b | c)
        self.assertTrue(a.is_cheapest)
        self.assertTrue(b.is_cheapest)
        self.assertFalse(c.is_cheapest)

    # ---- selection persistence ----
    def test_selection_persisted_and_survives_reload(self):
        a = self._make_line(18000, "2-3 days", code="jne")
        b = self._make_line(15000, "3-4 days", code="jnt", name="J&T")
        self._refresh(a | b)

        self.picking.write({"selected_delivery_estimate_id": b.id})
        self._refresh(a | b)

        self.assertTrue(b.is_selected)
        self.assertFalse(a.is_selected)
        self.assertEqual(
            self.picking.selected_delivery_estimate_id.id, b.id
        )
        self.assertEqual(
            self.picking.selected_delivery_estimate_id.courier_code, "jnt"
        )


class TestDeliveryCostActions(TransactionCase):
    """The two buttons wired to the "Delivery Cost" tab."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.Provider = cls.env["delivery.tracking.provider"]
        cls.Icp = cls.env["ir.config_parameter"]
        cls.out_type = cls.env.ref("stock.picking_type_out")
        cls.partner = cls.env["res.partner"].create(
            {
                "name": "Test Customer",
                "street": "Jl. Sudirman No. 1",
                "city": "Jakarta",
                "zip": "12345",
            }
        )
        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Product",
                "type": "consu",
                "weight": 1.0,
                "list_price": 50000,
            }
        )

    def setUp(self):
        super().setUp()
        self.provider = self.Provider.create(
            {
                "name": "Biteship",
                "api_base_url": "https://api.biteship.com",
                "api_key": "biteship_test.KEY",
            }
        )
        self.Icp.set_param(ACTIVE_COURIERS, "jne,jnt")
        self.Icp.set_param(COURIERS_UPDATED, datetime.now().isoformat())
        self.Icp.set_param(DEFAULT_DIMS, "5x5x5")

    def _make_outgoing(self):
        picking = self.Picking.create(
            {"picking_type_id": self.out_type.id, "partner_id": self.partner.id}
        )
        self.env["stock.move"].create(
            {
                "name": self.product.name,
                "product_id": self.product.id,
                "product_uom_qty": 2,
                "product_uom": self.product.uom_id.id,
                "picking_id": picking.id,
                "location_id": picking.location_id.id,
                "location_dest_id": picking.location_dest_id.id,
            }
        )
        return picking

    def test_action_estimate_success(self):
        picking = self._make_outgoing()
        self.Icp.set_param(ORIGIN_AREA, "")
        with patch(
            SEARCH_AREAS,
            return_value=[{"id": "ORIG-X", "postal_code": "12345"}],
        ), patch(
            RATES,
            return_value={"success": True, "pricing": PRICING_OK},
        ):
            action = picking.action_estimate_delivery_cost()

        self.assertEqual(len(picking.delivery_cost_estimate_ids), 2)
        self.assertTrue(picking.delivery_cost_estimated_at)
        self.assertFalse(picking.selected_delivery_estimate_id)
        self.assertEqual(action["params"]["type"], "success")
        # cheapest/fastest flags resolve on the saved lines
        picking.delivery_cost_estimate_ids.invalidate_recordset()
        cheapest = picking.delivery_cost_estimate_ids.filtered(
            lambda l: l.courier_code == "jnt"
        )
        self.assertTrue(cheapest.is_cheapest)

    def test_action_use_delivery_estimate_marks_selection(self):
        picking = self._make_outgoing()
        with patch(
            SEARCH_AREAS,
            return_value=[{"id": "ORIG-X", "postal_code": "12345"}],
        ), patch(
            RATES,
            return_value={"success": True, "pricing": PRICING_OK},
        ):
            picking.action_estimate_delivery_cost()
        line = picking.delivery_cost_estimate_ids[0]
        action = line.action_use_delivery_estimate()
        picking.invalidate_recordset()
        self.assertEqual(
            picking.selected_delivery_estimate_id.id, line.id
        )
        self.assertTrue(line.is_selected)
        self.assertEqual(action["params"]["type"], "success")

    def test_action_estimate_non_outgoing_error(self):
        picking = self.Picking.create(
            {"picking_type_id": self.env.ref("stock.picking_type_in").id}
        )
        with self.assertRaises(UserError) as cm:
            picking.action_estimate_delivery_cost()
        self.assertIn("outgoing", str(cm.exception))

    def test_action_estimate_no_provider_error(self):
        picking = self._make_outgoing()
        self.Provider.search([]).unlink()
        with self.assertRaises(UserError) as cm:
            picking.action_estimate_delivery_cost()
        self.assertIn("Biteship", str(cm.exception))

    def test_action_estimate_incomplete_address_error(self):
        """Address without street/city fails with a clear message pre-API."""
        incomplete = self.env["res.partner"].create(
            {"name": "Incomplete Customer"}
        )
        picking = self.Picking.create(
            {
                "picking_type_id": self.out_type.id,
                "partner_id": incomplete.id,
            }
        )
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        # Even if the API would return areas, the missing address must stop us.
        with patch(SEARCH_AREAS) as m:
            with self.assertRaises(UserError) as cm:
                picking.action_estimate_delivery_cost()
        m.assert_not_called()
        self.assertIn("Jalan", str(cm.exception))
        self.assertIn("Kota", str(cm.exception))

    def test_action_estimate_destination_incomplete_error(self):
        picking = self._make_outgoing()
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        with patch(SEARCH_AREAS, return_value=[]):
            with self.assertRaises(UserError) as cm:
                picking.action_estimate_delivery_cost()
        self.assertIn("tujuan", str(cm.exception))

    def test_action_estimate_api_error_surfaces_message(self):
        picking = self._make_outgoing()
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        with patch(
            SEARCH_AREAS,
            return_value=[{"id": "DEST-1", "postal_code": "12345"}],
        ), patch(
            RATES,
            return_value={
                "success": False,
                "error": "HTTP 500: Server Error",
                "pricing": [],
            },
        ):
            with self.assertRaises(UserError) as cm:
                picking.action_estimate_delivery_cost()
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertNotIn("KEY", str(cm.exception))

    def test_action_estimate_no_pricing_error(self):
        picking = self._make_outgoing()
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        with patch(
            SEARCH_AREAS,
            return_value=[{"id": "DEST-1", "postal_code": "12345"}],
        ), patch(
            RATES,
            return_value={"success": True, "pricing": []},
        ):
            with self.assertRaises(UserError) as cm:
                picking.action_estimate_delivery_cost()
        self.assertIn("tersedia", str(cm.exception))