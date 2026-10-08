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
    "models.delivery_cost_estimate_mixin.BiteshipRatesClient"
)
SEARCH_AREAS = CLIENT_PATH + ".search_areas"
GET_COURIERS = CLIENT_PATH + ".get_couriers"

ORIGIN_AREA = "delivery_cost_estimator.origin_area_id"
ACTIVE_COURIERS = "delivery_cost_estimator.active_couriers"
COURIERS_UPDATED = "delivery_cost_estimator.active_couriers_updated"
DEFAULT_DIMS = "delivery_cost_estimator.default_item_dimensions"

RATES = CLIENT_PATH + ".get_rates"

ORIGIN_ZIP = "57731"
DEST_ZIP = "12345"


def area(area_id, district, city, province, postal, level_3=None):
    """Build a Biteship ``/v1/maps/areas`` row.

    Mirrors the real payload: the postal code only appears as the trailing
    token of ``name`` and there is no ``postal_code`` field.
    """
    return {
        "id": area_id,
        "name": "%s, %s, %s. %s" % (district, city, province, postal),
        "country_name": "Indonesia",
        "country_code": "ID",
        "administrative_division_level_1_name": province,
        "administrative_division_level_1_type": "province",
        "administrative_division_level_2_name": city,
        "administrative_division_level_2_type": "city",
        "administrative_division_level_3_name": level_3 or district,
        "administrative_division_level_3_type": "district",
    }


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
        # Pin the warehouse address so origin resolution is deterministic.
        warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.origin_partner = warehouse.partner_id
        cls.origin_partner.write(
            {
                "street": "Jl. Songgorunggi RT 002, Dagen, Kec. Jaten",
                "city": "Karanganyar",
                "zip": ORIGIN_ZIP,
            }
        )

    def _origin_areas(self):
        return [
            area("ORIG-OTHER", "Ngargoyoso", "Karanganyar", "Jawa Tengah", ORIGIN_ZIP),
            area("ORIG-X", "Jaten", "Karanganyar", "Jawa Tengah", ORIGIN_ZIP),
        ]

    def _dest_areas(self):
        return [
            area("DEST-OTHER", "Panggungrejo", "Pasuruan", "Jawa Timur", "67111"),
            area("DEST-1", "Gempol", "Pasuruan", "Jawa Timur", DEST_ZIP),
        ]

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

    # ---- weight basis ----
    def test_weight_basis_uses_reserved_qty_when_not_done(self):
        """A reserved but unprocessed bulk line must not count as 0 kg."""
        picking = self._make_picking()  # 2 kg x 3, quantity_done = 0
        billable_kg, source, product_kg = picking._dce_weight_basis()
        self.assertEqual(source, "product")
        self.assertEqual(product_kg, 6.0)
        self.assertEqual(billable_kg, 6)

    def test_weight_basis_rounds_up_to_whole_kilograms(self):
        self.product.write({"weight": 0.5})
        picking = self._make_picking()  # 0.5 kg x 3 = 1.5 kg -> 2 kg
        billable_kg, _source, product_kg = picking._dce_weight_basis()
        self.assertEqual(product_kg, 1.5)
        self.assertEqual(billable_kg, 2)
        total = sum(
            i["weight"] * i["quantity"] for i in picking._build_delivery_items()
        )
        # Rounded down on purpose: Biteship rounds the parcel up to whole
        # kilograms, so staying under 2 kg still prices as 2 kg.
        self.assertLessEqual(total, 2000)
        self.assertGreater(total, 1900)

    def test_cancelled_moves_do_not_add_weight(self):
        """A released line no longer ships, so it must not inflate the quote."""
        self.product.write({"weight": 2.0})
        picking = self._make_picking()  # 2 kg x 3 = 6 kg
        extra = self.env["stock.move"].create(
            {
                "name": "released line",
                "product_id": self.product.id,
                "product_uom_qty": 5,
                "product_uom": self.product.uom_id.id,
                "picking_id": picking.id,
                "location_id": self.env.ref(
                    "stock.stock_location_customers"
                ).id,
                "location_dest_id": self.env.ref(
                    "stock.stock_location_suppliers"
                ).id,
                "state": "cancel",
            }
        )
        self.assertIn(extra, picking.move_ids)
        self.assertNotIn(extra, picking._dce_weight_moves())
        self.assertEqual(picking._dce_product_weight_kg(), 6.0)
        self.assertEqual(picking._dce_weight_basis()[0], 6)
        total = sum(
            i["weight"] * i["quantity"] for i in picking._build_delivery_items()
        )
        self.assertLessEqual(total, 6000)

    def test_scaled_items_never_exceed_the_target_weight(self):
        """One gram over a kg boundary would jump a whole price bracket."""
        items = [{"name": "x", "quantity": 3.0, "weight": 500}]
        self.Picking._dce_scale_items_to_weight(items, 2000)
        self.assertLessEqual(
            sum(i["weight"] * i["quantity"] for i in items), 2000
        )

    def test_weight_basis_weight_total_field_wins(self):
        picking = self._make_picking()
        if "weight_total" not in picking._fields:
            self.skipTest("raja_ongkir not installed")
        picking.write({"weight_total": 12})
        billable_kg, source, product_kg = picking._dce_weight_basis()
        self.assertEqual(source, "weight_total")
        self.assertEqual(billable_kg, 12)
        self.assertEqual(product_kg, 6.0)
        total = sum(
            i["weight"] * i["quantity"] for i in picking._build_delivery_items()
        )
        self.assertLessEqual(total, 12000)
        self.assertGreater(total, 11000)

    def test_weight_total_can_lower_a_heavier_product(self):
        """A hand-entered parcel weight smaller than the products must apply."""
        picking = self._make_picking()
        if "weight_total" not in picking._fields:
            self.skipTest("raja_ongkir not installed")
        picking.write({"weight_total": 1})
        total = sum(
            i["weight"] * i["quantity"] for i in picking._build_delivery_items()
        )
        self.assertLessEqual(total, 1000)
        self.assertGreater(total, 900)

    def test_build_items_weightless_products_get_the_declared_weight(self):
        self.product.write({"weight": 0.0})
        picking = self._make_picking()
        if "weight_total" not in picking._fields:
            self.skipTest("raja_ongkir not installed")
        picking.write({"weight_total": 4})
        total = sum(
            i["weight"] * i["quantity"] for i in picking._build_delivery_items()
        )
        self.assertLessEqual(total, 4000)
        self.assertGreater(total, 3800)

    def test_quantity_done_overrides_reserved_qty(self):
        picking = self._make_picking()
        move = picking.move_ids[0]
        move.quantity_done = 1  # only one unit really shipped
        billable_kg, _source, product_kg = picking._dce_weight_basis()
        self.assertEqual(product_kg, 2.0)
        self.assertEqual(billable_kg, 2)
        self.assertEqual(picking._build_delivery_items()[0]["quantity"], 1.0)

    # ---- courier coverage warning ----
    def test_warns_about_couriers_without_a_rate(self):
        picking = self._make_picking()
        lines = self.Picking._parse_pricing(
            [{"courier_code": "jne", "price": 9000}]
        )
        warnings = picking._dce_estimate_warnings(
            ["jne", "jnt", "sicepat"], lines
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("jnt", warnings[0])
        self.assertIn("sicepat", warnings[0])

    def test_no_courier_warning_when_all_quoted(self):
        picking = self._make_picking()
        lines = self.Picking._parse_pricing(
            [{"courier_code": "jne", "price": 9000},
             {"courier_code": "jnt", "price": 8000}]
        )
        self.assertEqual(
            picking._dce_estimate_warnings(["jne", "jnt"], lines), []
        )

    def test_warns_when_manual_weight_contradicts_products(self):
        picking = self._make_picking()
        if "weight_total" not in picking._fields:
            self.skipTest("raja_ongkir not installed")
        picking.write({"weight_total": 30})  # products only add up to 6 kg
        lines = self.Picking._parse_pricing([{"courier_code": "jne", "price": 9000}])
        warnings = picking._dce_estimate_warnings(["jne"], lines)
        self.assertTrue(any("30" in w for w in warnings))

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
        with patch(SEARCH_AREAS, return_value=self._origin_areas()):
            area_id = picking._resolve_origin_area_id()
        self.assertEqual(area_id, "ORIG-X")
        self.assertEqual(self.Icp.get_param(ORIGIN_AREA), "ORIG-X")

    def test_origin_unmatched_postal_is_not_guessed(self):
        """A fuzzy hit for the wrong district must not be cached as origin."""
        self.Icp.set_param(ORIGIN_AREA, "")
        picking = self._make_picking()
        unrelated = [
            area("X-1", "Denpasar Barat", "Denpasar", "Bali", "80111"),
        ]
        with patch(SEARCH_AREAS, return_value=unrelated):
            self.assertIsNone(picking._resolve_origin_area_id())
        self.assertFalse(self.Icp.get_param(ORIGIN_AREA))

    # ---- destination area caching on partner ----
    def test_destination_cached_on_partner(self):
        picking = self._make_picking()
        with patch(SEARCH_AREAS, return_value=self._dest_areas()) as m:
            self.assertEqual(
                picking._get_delivery_destination_area_id(), "DEST-1"
            )
        self.assertEqual(self.partner.biteship_area_id, "DEST-1")
        # second call reuses the partner cache, never hits the network
        with patch(SEARCH_AREAS) as m2:
            self.assertEqual(
                picking._get_delivery_destination_area_id(), "DEST-1"
            )
        m2.assert_not_called()
        self.assertEqual(m.call_count, 1)

    def test_destination_unresolvable_returns_none(self):
        picking = self._make_picking()
        with patch(SEARCH_AREAS, return_value=[]):
            self.assertIsNone(picking._get_delivery_destination_area_id())
        self.assertFalse(self.partner.biteship_area_id)

    def test_destination_city_only_match_is_rejected(self):
        """No zip and no district -> refuse instead of quoting a random zone."""
        partner = self.env["res.partner"].create(
            {
                "name": "No Zip Customer",
                "street": "Jl. Raya 1",
                "city": "Pasuruan",
            }
        )
        picking = self.Picking.create(
            {
                "picking_type_id": self.out_type.id,
                "partner_id": partner.id,
            }
        )
        with patch(SEARCH_AREAS, return_value=self._dest_areas()):
            self.assertIsNone(picking._get_delivery_destination_area_id())
        self.assertFalse(partner.biteship_area_id)

    def test_destination_matches_district_without_zip(self):
        partner = self.env["res.partner"].create(
            {
                "name": "Kec Customer",
                "street": "Jl. Raya Wonoayu No. 26, Kec. Gempol",
                "city": "Pasuruan",
            }
        )
        picking = self.Picking.create(
            {
                "picking_type_id": self.out_type.id,
                "partner_id": partner.id,
            }
        )
        with patch(SEARCH_AREAS, return_value=self._dest_areas()):
            self.assertEqual(
                picking._get_delivery_destination_area_id(), "DEST-1"
            )
        self.assertEqual(partner.biteship_area_id, "DEST-1")

    def test_area_postal_code_parsed_from_name(self):
        row = area("A", "Gempol", "Pasuruan", "Jawa Timur", "67155")
        self.assertEqual(self.Picking._dce_area_postal_code(row), "67155")
        self.assertEqual(self.Picking._dce_area_district(row), "Gempol")

    def test_pick_area_rejects_ambiguous_shared_postal(self):
        """One postal code spanning two districts is not decisive on its own."""
        rows = [
            area("S-1", "Jaten", "Karanganyar", "Jawa Tengah", ORIGIN_ZIP),
            area("S-2", "Ngargoyoso", "Karanganyar", "Jawa Tengah", ORIGIN_ZIP),
        ]
        self.assertIsNone(self.Picking._dce_pick_area(rows, ORIGIN_ZIP, ""))
        self.assertEqual(
            self.Picking._dce_pick_area(rows, ORIGIN_ZIP, "Ngargoyoso")["id"],
            "S-2",
        )

    def test_pick_area_prefers_exact_postal_over_first_row(self):
        rows = self._dest_areas()
        self.assertEqual(rows[0]["id"], "DEST-OTHER")
        picked = self.Picking._dce_pick_area(rows, DEST_ZIP, "")
        self.assertEqual(picked["id"], "DEST-1")

    def test_pick_area_ignores_rows_without_postal_field(self):
        """The old code compared ``area['postal_code']``, absent in the API."""
        rows = [{"id": "A-1", "name": "Gempol, Pasuruan, Jawa Timur. 67155"}]
        self.assertEqual(
            self.Picking._dce_pick_area(rows, "67155", "")["id"], "A-1"
        )

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
            "odoo.addons.delivery_cost_estimator.models."
            "delivery_cost_estimate_mixin."
            "DeliveryCostEstimateMixin._dce_client",
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
        warehouse = cls.env["stock.warehouse"].search([], limit=1)
        warehouse.partner_id.write(
            {"city": "Karanganyar", "zip": ORIGIN_ZIP}
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

    def _origin_areas(self):
        return [
            area("ORIG-OTHER", "Ngargoyoso", "Karanganyar", "Jawa Tengah", ORIGIN_ZIP),
            area("ORIG-X", "Jaten", "Karanganyar", "Jawa Tengah", ORIGIN_ZIP),
        ]

    def _dest_areas(self):
        return [
            area("DEST-OTHER", "Panggungrejo", "Pasuruan", "Jawa Timur", "67111"),
            area("DEST-1", "Gempol", "Pasuruan", "Jawa Timur", DEST_ZIP),
        ]

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
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        with patch(
            SEARCH_AREAS,
            return_value=self._dest_areas(),
        ), patch(
            RATES,
            return_value={"success": True, "pricing": PRICING_OK},
        ):
            action = picking.action_estimate_delivery_cost()

        self.assertEqual(len(picking.delivery_cost_estimate_ids), 2)
        self.assertTrue(picking.delivery_cost_estimated_at)
        self.assertFalse(picking.selected_delivery_estimate_id)
        self.assertEqual(action["tag"], "reload")
        # cheapest/fastest flags resolve on the saved lines
        picking.delivery_cost_estimate_ids.invalidate_recordset()
        cheapest = picking.delivery_cost_estimate_ids.filtered(
            lambda l: l.courier_code == "jnt"
        )
        self.assertTrue(cheapest.is_cheapest)

    def test_action_use_delivery_estimate_marks_selection(self):
        picking = self._make_outgoing()
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        with patch(
            SEARCH_AREAS,
            return_value=self._dest_areas(),
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
        self.assertEqual(action["tag"], "reload")

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
        self.assertIn("kode pos", str(cm.exception))

    def test_action_estimate_rejects_wrong_zone_instead_of_guessing(self):
        """City-only fuzzy hit must not silently price an arbitrary district."""
        picking = self._make_outgoing()
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        unrelated = [
            area("Z-1", "Denpasar Barat", "Denpasar", "Bali", "80111"),
            area("Z-2", "Denpasar Selatan", "Denpasar", "Bali", "80211"),
        ]
        with patch(SEARCH_AREAS, return_value=unrelated), patch(
            RATES
        ) as rates:
            with self.assertRaises(UserError) as cm:
                picking.action_estimate_delivery_cost()
        rates.assert_not_called()
        self.assertIn("kode pos", str(cm.exception))

    def test_action_estimate_uses_district_when_zip_is_shared(self):
        """57731 covers Jaten *and* Ngargoyoso; the district must decide."""
        picking = self._make_outgoing()
        self.partner.write(
            {
                "street": "Jl. Raya Wonoayu No. 26, Kec. Gempol",
                "city": "Pasuruan",
                "zip": "67155",
            }
        )
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        shared_zip = [
            area("Z-A", "Ngargoyoso", "Karanganyar", "Jawa Tengah", "67155"),
            area("Z-B", "Gempol", "Pasuruan", "Jawa Timur", "67155"),
        ]
        with patch(SEARCH_AREAS, return_value=shared_zip), patch(
            RATES,
            return_value={"success": True, "pricing": PRICING_OK},
        ) as rates:
            picking.action_estimate_delivery_cost()
        self.assertEqual(self.partner.biteship_area_id, "Z-B")
        self.assertEqual(
            rates.call_args[0][1],
            "Z-B",
        )

    def test_action_estimate_api_error_surfaces_message(self):
        picking = self._make_outgoing()
        self.Icp.set_param(ORIGIN_AREA, "ORIG-CONFIG")
        with patch(
            SEARCH_AREAS,
            return_value=self._dest_areas(),
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
            return_value=self._dest_areas(),
        ), patch(
            RATES,
            return_value={"success": True, "pricing": []},
        ):
            with self.assertRaises(UserError) as cm:
                picking.action_estimate_delivery_cost()
        self.assertIn("tersedia", str(cm.exception))