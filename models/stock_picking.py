# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import logging
from datetime import datetime

from odoo import _, fields, models
from odoo.exceptions import UserError

from odoo.addons.delivery_cost_estimator.services.biteship_client import (
    BiteshipRatesClient,
)

_logger = logging.getLogger(__name__)

DCE_ORIGIN_AREA = "delivery_cost_estimator.origin_area_id"
DCE_ACTIVE_COURIERS = "delivery_cost_estimator.active_couriers"
DCE_COURIERS_UPDATED = "delivery_cost_estimator.active_couriers_updated"
DCE_DEFAULT_DIMS = "delivery_cost_estimator.default_item_dimensions"
DCE_CACHE_TTL = "delivery_cost_estimator.cache_ttl_hours"

DEFAULT_ITEM_DIMENSIONS = (10, 10, 10)
DEFAULT_CACHE_TTL_HOURS = 24


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    delivery_cost_estimate_ids = fields.One2many(
        'delivery.cost.estimate.line',
        'picking_id',
        string='Delivery Cost Estimates',
    )
    selected_delivery_estimate_id = fields.Many2one(
        'delivery.cost.estimate.line',
        string='Selected Delivery Estimate',
        domain="[('picking_id', '=', id)]",
    )
    delivery_cost_estimated_at = fields.Datetime(
        string='Delivery Cost Estimated At',
    )

    # ---------------------------------------------------------------------
    # Biteship provider & config plumbing (single origin for v1)
    # ---------------------------------------------------------------------

    def _dce_provider(self):
        """Return the active Biteship provider record, or an empty recordset.

        Credentials live in ``delivery.tracking.provider`` (same record used by
        ``stock_picking_delivery_tracking``) to keep one single source of truth.
        """
        return self.env['delivery.tracking.provider'].search(
            [('name', '=', 'Biteship')],
            limit=1,
        )

    def _dce_client(self):
        """Return a BiteshipRatesClient bound to the Biteship provider."""
        provider = self._dce_provider()
        if not provider:
            return None
        return BiteshipRatesClient(provider)

    def _dce_get_param(self, key, default=None):
        value = self.env['ir.config_parameter'].get_param(key)
        if value in (None, False, ''):
            return default
        return value

    def _dce_default_dimensions(self):
        """Return (length, width, height) cm used when a product has no dims.

        Configured via ``default_item_dimensions`` as ``LxWxH`` or ``LxWxH (cm)``.
        """
        raw = str(
            self._dce_get_param(DCE_DEFAULT_DIMS, None) or "10x10x10"
        ).lower()
        for sep in ('x', ' * ', ',', ' '):
            if sep in raw:
                parts = [p.strip() for p in raw.split(sep)]
                values = []
                for part in parts[:3]:
                    cleaned = ''.join(ch for ch in part if ch.isdigit() or ch == '.')
                    try:
                        values.append(float(cleaned))
                    except ValueError:
                        values.append(None)
                if all(value is not None and value > 0 for value in values):
                    return tuple(values)
        return DEFAULT_ITEM_DIMENSIONS

    def _dce_active_couriers_param(self):
        return self._dce_get_param(DCE_ACTIVE_COURIERS, '')

    def _dce_ttl_hours(self):
        try:
            return float(self._dce_get_param(DCE_CACHE_TTL, DEFAULT_CACHE_TTL_HOURS))
        except (TypeError, ValueError):
            return DEFAULT_CACHE_TTL_HOURS

    # ---------------------------------------------------------------------
    # Area resolution (origin single-from-config, destination cached)
    # ---------------------------------------------------------------------

    @staticmethod
    def _dce_pick_area(areas, postal_code=None):
        """Pick the best area from a Biteship areas result.

        Prefers an exact postal-code match, otherwise returns the first entry.
        """
        if not areas:
            return None
        if postal_code:
            for area in areas:
                if area.get('postal_code') == str(postal_code):
                    return area
        return areas[0]

    def _dce_origin_address(self):
        """Return (query, postal_code) of the configured single-origin address.

        Falls back to the first warehouse of the company, then the company
        partner, then the main company itself.
        """
        self.ensure_one()
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.company_id.id)],
            limit=1,
        )
        partner = (
            warehouse.partner_id
            if warehouse and warehouse.partner_id
            else self.company_id.partner_id
        )
        if not partner:
            return "", None
        return self._dce_address_query(partner), partner.zip or None

    @staticmethod
    def _dce_address_query(partner):
        for value in (
            partner.zip,
            partner.city,
            partner.state_id.name if partner.state_id else None,
        ):
            if value:
                return value
        address = partner.street or partner.street2 or ''
        if address:
            return address
        return partner.name or ''

    def _resolve_origin_area_id(self):
        """Resolve the single origin ``area_id``, persisting it afterwards."""
        self.ensure_one()
        configured = self._dce_get_param(DCE_ORIGIN_AREA, None)
        if configured:
            return configured
        client = self._dce_client()
        if not client:
            return None
        query, postal_code = self._dce_origin_address()
        if not query:
            return None
        areas = client.search_areas(query, postal_code=postal_code)
        area = self._dce_pick_area(areas, postal_code)
        if area and area.get('id'):
            self.env['ir.config_parameter'].set_param(
                DCE_ORIGIN_AREA,
                area['id'],
            )
            return area['id']
        return None

    def _get_delivery_destination_area_id(self):
        """Resolve and cache the destination ``area_id`` on ``res.partner``."""
        self.ensure_one()
        partner = self.partner_id
        if not partner:
            return None
        if partner.biteship_area_id:
            return partner.biteship_area_id
        query = self._dce_address_query(partner)
        if not query:
            return None
        client = self._dce_client()
        if not client:
            return None
        areas = client.search_areas(query, postal_code=partner.zip or None)
        area = self._dce_pick_area(areas, partner.zip or None)
        if area and area.get('id'):
            partner.write({'biteship_area_id': area['id']})
            return area['id']
        return None

    def _dce_missing_address_parts(self):
        """Return the list of missing required destination address parts.

        Biteship recommends area_id resolution over postal codes, so the
        destination must carry at least a street and a city; zip and state are
        optional supplements (they tighten the match when present).
        """
        self.ensure_one()
        partner = self.partner_id
        if not partner:
            return ['Customer']
        missing = []
        if not (partner.street or partner.street2):
            missing.append('Jalan')
        if not partner.city:
            missing.append('Kota')
        return missing

    # ---------------------------------------------------------------------
    # Active couriers (cached with configurable TTL)
    # ---------------------------------------------------------------------

    def _get_active_couriers(self):
        """Return the list of courier codes to compare.

        Uses ``active_couriers`` when fresh (per ``cache_ttl_hours``), otherwise
        refreshes from ``GET /v1/couriers`` and persists both codes and timestamp.
        """
        config = self.env['ir.config_parameter']
        codes = self._dce_active_couriers_param()
        if codes:
            updated_raw = config.get_param(DCE_COURIERS_UPDATED)
            try:
                updated = datetime.fromisoformat(updated_raw) if updated_raw else None
            except (TypeError, ValueError):
                updated = None
            now = datetime.now()
            if updated and (now - updated).total_seconds() < self._dce_ttl_hours() * 3600:
                return [code.strip() for code in codes.split(',') if code.strip()]
        client = self._dce_client()
        if not client:
            return []
        couriers = client.get_couriers()
        active = [
            courier.get('courier_code') or courier.get('code')
            for courier in couriers
            if courier.get('courier_code') or courier.get('code')
        ]
        config.set_param(DCE_ACTIVE_COURIERS, ','.join(active))
        config.set_param(DCE_COURIERS_UPDATED, datetime.now().isoformat())
        return active

    # ---------------------------------------------------------------------
    # Items / weight / dimensions
    # ---------------------------------------------------------------------

    def _build_delivery_items(self):
        """Build the Biteship ``items`` payload from the picking's moves.

        One item per distinct product. Weight is converted from the product's
        kg ``weight`` to grams; dimensions come from the product when available,
        otherwise from the configurable fallback. Always returns at least 1 gram
        of total weight so the API never receives a zero-weight parcel.
        """
        self.ensure_one()
        default_dims = self._dce_default_dimensions()
        has_dims_field = 'length' in self.env['product.product']._fields
        items = {}
        moves = self.move_ids.filtered(
            lambda move: move.product_id and (move.product_uom_qty or move.product_qty)
        )
        for move in moves:
            product = move.product_id
            qty = float(move.product_uom_qty or move.product_qty or 1)
            weight_grams = max(int(round((product.weight or 0.0) * 1000)), 0)
            if (
                has_dims_field
                and product.length
                and product.width
                and product.height
            ):
                length, width, height = (
                    (float(product.length), float(product.width), float(product.height))
                )
            else:
                length, width, height = default_dims
            value = (
                move.sale_line_id.price_unit
                if move.sale_line_id and move.sale_line_id.price_unit
                else (product.list_price or 0.0)
            )
            if product.id in items:
                items[product.id]['quantity'] += qty
                items[product.id]['value'] += value * qty
            else:
                items[product.id] = {
                    'name': product.name or 'Barang',
                    'description': 'Kategori: %s' % (
                        product.categ_id.name or 'Umum'
                    ),
                    'value': value * qty,
                    'quantity': qty,
                    'weight': weight_grams,
                    'length': length,
                    'width': width,
                    'height': height,
                }
        result = list(items.values())
        if result:
            total_weight = sum(
                item['weight'] * item['quantity'] for item in result
            )
            if total_weight < 1:
                result[0]['quantity'] = max(result[0]['quantity'], 1)
                result[0]['weight'] = 1
        return result

    @staticmethod
    def _parse_pricing(pricing):
        """Parse a Biteship ``pricing`` array into line dicts for the O2M."""
        lines = []
        for pricing_line in pricing or []:
            if not isinstance(pricing_line, dict):
                continue
            try:
                price = float(pricing_line.get('price') or 0)
            except (TypeError, ValueError):
                price = 0
            lines.append({
                'courier_code': pricing_line.get('courier_code') or '',
                'courier_name': pricing_line.get('courier_name') or '',
                'service_code': pricing_line.get('courier_service_code') or '',
                'service_name': pricing_line.get('courier_service_name') or '',
                'price': price,
                'duration_text': pricing_line.get('duration') or '',
            })
        return lines

    # ---------------------------------------------------------------------
    # User-facing actions (wired to the "Delivery Cost" tab buttons)
    # ---------------------------------------------------------------------

    def action_estimate_delivery_cost(self):
        """Compute the multi-courier cost estimates for this delivery order.

        Explicit user action only (never called on view load) so Biteship is
        only contacted on request. Errors are surfaced as clear UserError
        messages (never tracebacks) and never include the API key.
        """
        self.ensure_one()
        if self.picking_type_code != 'outgoing':
            raise UserError(
                _('Estimasi ongkir hanya tersedia untuk Delivery Order (picking outgoing).')
            )
        client = self._dce_client()
        if not client:
            raise UserError(
                _('Provider Biteship belum dikonfigurasi '
                  '(cek pengaturan "Delivery Tracking / Delivery Cost" di Settings).')
            )
        origin_area = self._resolve_origin_area_id()
        if not origin_area:
            raise UserError(
                _('Origin pengiriman tidak bisa di-resolve ke area Biteship. '
                  'Lengkapi alamat gudang/company atau set "Origin Area ID" '
                  'di pengaturan Delivery Cost Estimator.')
            )
        missing_address_parts = self._dce_missing_address_parts()
        if missing_address_parts:
            raise UserError(
                _('Alamat tujuan (customer) belum lengkap: %s. '
                  'Lengkapi alamat partner tujuan sebelum menghitung ongkir.')
                % ', '.join(missing_address_parts)
            )
        destination_area = self._get_delivery_destination_area_id()
        if not destination_area:
            raise UserError(
                _('Alamat tujuan (customer) belum lengkap atau tidak ditemukan '
                  'di area Biteship. Lengkapi nama jalan, kota, dan kode pos '
                  'pada partner tujuan.')
            )
        items = self._build_delivery_items()
        if not items:
            raise UserError(
                _('Delivery Order ini tidak memiliki baris produk '
                  'dengan kuantitas untuk dihitung ongkirnya.')
            )
        couriers = self._get_active_couriers()
        if not couriers:
            raise UserError(
                _('Daftar kurir aktif kosong. Ambil daftar kurir dari '
                  'pengaturan Delivery Cost Estimator, lalu simpan.')
            )
        result = client.get_rates(
            origin_area,
            destination_area,
            couriers,
            items,
        )
        if not result.get('success'):
            raise UserError(
                _('Estimasi ongkir gagal dihitung: %s') % (
                    result.get('error') or 'Gagal terhubung ke Biteship.',
                )
            )
        pricing = result.get('pricing') or []
        if not pricing:
            raise UserError(
                _('Tidak ada layanan kurir yang tersedia untuk alamat & berat '
                  'paket ini. Coba ubah alamat tujuan atau tambah kurir aktif.')
            )
        lines = self._parse_pricing(pricing)
        self.delivery_cost_estimate_ids = (
            [(5, 0, 0)] + [(0, 0, line) for line in lines]
        )
        self.write({
            'delivery_cost_estimated_at': fields.Datetime.now(),
            'selected_delivery_estimate_id': False,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': _('%d penawaran ongkir dari %d kurir berhasil dihitung.') % (
                    len(lines),
                    len({line['courier_code'] for line in lines if line['courier_code']}),
                ),
                'sticky': False,
            },
        }