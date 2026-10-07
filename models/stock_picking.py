# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import logging
import math
import re
from datetime import datetime
from html import escape

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


def _dce_format_kg(value):
    """Render a kilogram figure without trailing ``.0`` noise."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return '%d' % int(number)
    return ('%.2f' % number).rstrip('0').rstrip('.')


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
    def _dce_area_postal_code(area):
        """Extract the postal code of a Biteship area.

        ``GET /v1/maps/areas`` returns **no** ``postal_code`` field; the code is
        only the trailing token of ``name`` (``"Gempol, Pasuruan, Jawa Timur.
        67155"``). Comparing ``area['postal_code']`` therefore never matched and
        every lookup silently degraded to ``areas[0]``.
        """
        name = str(area.get('name') or '')
        token = name.rsplit(' ', 1)[-1].strip()
        return token if token.isdigit() else None

    @staticmethod
    def _dce_area_district(area):
        """Return the district ("kecamatan") name of a Biteship area."""
        for key in ('administrative_division_level_3_name',):
            value = str(area.get(key) or '').strip()
            if value:
                return value
        name = str(area.get('name') or '')
        parts = [part.strip() for part in name.split(',')]
        return parts[0] if parts else ''

    @staticmethod
    def _dce_pick_area(areas, postal_code=None, district_name=None):
        """Pick the best area from a Biteship areas result, or ``None``.

        ``/v1/maps/areas`` is a *fuzzy* search: it returns several districts of
        the same city in an arbitrary order (asking for ``Karanganyar`` returns
        ten rows for a handful of districts, many of them duplicates). Taking
        ``areas[0]`` therefore priced an arbitrary -- often wrong -- zone, so
        candidates are scored instead:

        * a matching district ("Kec.") wins outright, since it is the most
          selective signal and is what couriers actually zone on;
        * otherwise a postal code that matches **exactly one** returned area is
          accepted;
        * an ambiguous postal hit, or a city-only hit, yields ``None`` so the
          caller reports an unusable address instead of quoting a wrong zone.

        One postal code regularly spans several districts (57731 covers both
        Jaten and Ngargoyoso), which is why a shared postal code alone is not
        treated as decisive.
        """
        if not areas:
            return None
        wanted_postal = str(postal_code or '').strip()
        wanted_district = str(district_name or '').strip().lower()
        district_hits = []
        postal_hits = []
        for area in areas:
            if wanted_district and (
                StockPicking._dce_area_district(area).lower() == wanted_district
            ):
                district_hits.append(area)
                continue
            if wanted_postal and (
                StockPicking._dce_area_postal_code(area) == wanted_postal
            ):
                postal_hits.append(area)
        if district_hits:
            return district_hits[0]
        if len(postal_hits) == 1:
            return postal_hits[0]
        return None

    @staticmethod
    def _dce_street_district(partner):
        """Return the district name written in a street address ("Kec. Gempol")."""
        for line in (partner.street or '', partner.street2 or ''):
            found = re.search(
                r'(?:kec(?:\.|amatan)?)[.\s]+([\w\-]+(?:\s[\w\-]+)?)',
                line or '',
                re.IGNORECASE,
            )
            if found:
                return found.group(1).strip()
        return ''

    def _dce_origin_address(self):
        """Return (query, postal_code, district) of the configured single origin.

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
            return "", None, ''
        return (
            self._dce_address_query(partner),
            partner.zip or None,
            self._dce_street_district(partner),
        )

    @staticmethod
    def _dce_address_query(partner):
        """Build the ``/v1/maps/areas`` search string for a partner.

        The postal code is the most selective token, so it leads; a district
        ("Kec.") name is appended when present to disambiguate streets that
        share one postal code.
        """
        district = StockPicking._dce_street_district(partner)
        city = partner.city or ''
        if district and partner.zip:
            return '%s %s' % (district, partner.zip)
        if partner.zip:
            return partner.zip
        if district:
            return '%s %s' % (district, city) if city else district
        if city:
            return city
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
        query, postal_code, district = self._dce_origin_address()
        if not query:
            return None
        areas = client.search_areas(query, postal_code=postal_code)
        area = self._dce_pick_area(areas, postal_code, district)
        if area and area.get('id'):
            self.env['ir.config_parameter'].set_param(
                DCE_ORIGIN_AREA,
                area['id'],
            )
            return area['id']
        return None

    def _get_delivery_destination_area_id(self):
        """Resolve and cache the destination ``area_id`` on ``res.partner``.

        Returns ``None`` when Biteship has no area matching the partner's postal
        code or district. The partner cache is only written for a confident
        match so an incomplete address can be completed and retried later.
        """
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
        area = self._dce_pick_area(
            areas,
            partner.zip or None,
            self._dce_street_district(partner),
        )
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
                result = [code.strip() for code in codes.split(',') if code.strip()]
                if result:
                    return result
        client = self._dce_client()
        if client:
            couriers = client.get_couriers()
            active = list(dict.fromkeys(
                courier.get('courier_code') or courier.get('code')
                for courier in couriers
                if courier.get('courier_code') or courier.get('code')
            ))
            if active:
                config.set_param(DCE_ACTIVE_COURIERS, ','.join(active))
                config.set_param(DCE_COURIERS_UPDATED, datetime.now().isoformat())
                return active
        # Fallback default couriers if API returns empty
        fallback = ['jne', 'jnt', 'sicepat', 'anteraja', 'ninja', 'lion', 'pos', 'tiki', 'wahana', 'idexpress', 'rex', 'first', 'star', 'ncs', 'sap', 'dse']
        config.set_param(DCE_ACTIVE_COURIERS, ','.join(fallback))
        config.set_param(DCE_COURIERS_UPDATED, datetime.now().isoformat())
        return fallback

    # ---------------------------------------------------------------------
    # Items / weight / dimensions
    # ---------------------------------------------------------------------

    def _dce_billable_quantity(self, move):
        """Quantity of a move that will actually ship.

        ``quantity_done`` is authoritative once a move is processed. Before
        that it is still 0 while Odoo has already reserved ``product_uom_qty``,
        so the reserved demand is the best available figure. Treating a
        not-yet-done bulk line as zero (as the Raja Ongkir tab used to) quoted
        40 printers as a 1 kg parcel.
        """
        return move.quantity_done or move.product_uom_qty or 0.0

    def _dce_weight_moves(self):
        """Moves whose weight counts towards the quoted parcel.

        ``move_ids_without_package`` (the same set ``raja_ongkir`` uses) keeps
        the two tabs of one Delivery Order from disagreeing, and cancelled
        moves are dropped: a line Odoo already released no longer ships, yet it
        was still adding weight (0.35 kg of a cancelled mouse on DO 3015).
        """
        moves = self.move_ids_without_package or self.move_ids
        return moves.filtered(lambda move: move.state != 'cancel')

    def _dce_product_weight_kg(self):
        """Total product weight of this Delivery Order in kg (quantity included)."""
        total = 0.0
        for move in self._dce_weight_moves():
            if not move.product_id:
                continue
            total += (move.product_id.weight or 0.0) * self._dce_billable_quantity(move)
        return total

    def _dce_weight_override_kg(self):
        """Weight typed by hand on the Raja Ongkir tab, or ``0.0`` when unset."""
        if 'weight_total' not in self._fields:
            return 0.0
        try:
            return float(self.weight_total or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _dce_weight_basis(self):
        """Return ``(billable_kg, source, product_kg)`` for the estimate.

        The rule is deliberately identical to ``raja_ongkir`` so the
        "Raja Ongkir" and "Delivery Cost" tabs of one Delivery Order can never
        quote two different weights for the same parcel:

        1. ``weight_total`` (hand-entered, authoritative) wins;
        2. otherwise the summed product weight rounded **up** to whole
           kilograms, matching how couriers bill;
        3. otherwise 1 kg, the courier minimum.

        ``product_kg`` is returned so the caller can warn when a manual weight
        contradicts the products it is supposed to weigh.
        """
        self.ensure_one()
        product_kg = self._dce_product_weight_kg()
        override_kg = self._dce_weight_override_kg()
        if override_kg > 0:
            return override_kg, 'weight_total', product_kg
        return max(math.ceil(product_kg), 1), 'product', product_kg

    @staticmethod
    def _dce_product_dimensions(product, default_dims):
        """Return ``(length, width, height)`` in cm for a product.

        Product dimensions are only used when a module really adds
        ``length``/``width``/``height`` (neither ``product.product`` nor
        ``product.template`` ships them in Odoo 16), so the configured default
        is what applies on a stock install.
        """
        for model in (product, product.product_tmpl_id):
            if all(name in model._fields for name in ('length', 'width', 'height')):
                dims = (
                    model.length,
                    model.width,
                    model.height,
                )
                if all(dims):
                    return tuple(float(value) for value in dims)
        return default_dims

    def _build_delivery_items(self):
        """Build the Biteship ``items`` payload from the picking's moves.

        One item per distinct product. ``weight`` is the weight of a **single
        unit in grams** because Biteship multiplies it by ``quantity``; the
        per-unit weights are rescaled when ``weight_total`` is set so the
        payload always adds up to the authoritative parcel weight. Dimensions
        fall back to the configured default. At least 1 gram of total weight is
        guaranteed so the API never receives a zero-weight parcel.
        """
        self.ensure_one()
        default_dims = self._dce_default_dimensions()
        target_kg, _source, _product_kg = self._dce_weight_basis()
        items = {}
        moves = self._dce_weight_moves().filtered(
            lambda move: move.product_id and self._dce_billable_quantity(move)
        )
        for move in moves:
            product = move.product_id
            qty = float(self._dce_billable_quantity(move))
            weight_grams = max(int(round((product.weight or 0.0) * 1000)), 0)
            length, width, height = self._dce_product_dimensions(product, default_dims)
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
        if not result:
            return result
        self._dce_scale_items_to_weight(result, target_kg * 1000)
        total_weight = sum(item['weight'] * item['quantity'] for item in result)
        if total_weight < 1:
            heaviest = max(result, key=lambda item: item['weight'])
            heaviest['weight'] = max(
                int(round(1 / max(heaviest['quantity'], 1))) or 1, 1
            )
        return result

    @staticmethod
    def _dce_scale_items_to_weight(items, target_grams):
        """Rescale per-unit item weights so the parcel totals ``target_grams``.

        Biteship bills ``weight x quantity`` per line and rounds the parcel up
        to whole kilograms, so a payload even one gram over a kilogram boundary
        jumps a whole price bracket. The rescale therefore always rounds
        *down*: the total lands just below the target and Biteship's own
        rounding brings it back to the intended kilogram. Per-line ratios are
        preserved whenever the products carry a weight.
        """
        target = int(round(target_grams or 0))
        if not items or target <= 0:
            return
        current = sum(item['weight'] * item['quantity'] for item in items)
        if current <= 0:
            # Nothing to scale: put the whole parcel on the bulkiest line.
            bulkiest = max(items, key=lambda item: item['quantity'])
            bulkiest['weight'] = max(
                int(target / max(bulkiest['quantity'], 1)), 1
            )
            return
        if current == target:
            return
        factor = float(target) / float(current)
        for item in items:
            item['weight'] = max(int(item['weight'] * factor), 1)

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
                _('Alamat tujuan (customer) tidak bisa di-petakan ke area '
                  'Biteship secara tepat. Biteship mencari alamat secara kabur, '
                  'jadi tanpa kode pos atau nama kecamatan (mis. "Kec. Gempol") '
                  'di alamat partner, zona ongkir bisa salah. Lengkapi kode pos '
                  'dan/atau kecamatan pada partner tujuan lalu coba lagi.')
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
        warnings = self._dce_estimate_warnings(couriers, lines)
        billable_kg, source, product_kg = self._dce_weight_basis()
        message = _('%d penawaran ongkir dari %d kurir '
                    'berhasil dihitung (berat %s kg).') % (
            len(lines),
            len({line['courier_code'] for line in lines if line['courier_code']}),
            _dce_format_kg(billable_kg),
        )
        if warnings:
            message = '%s\n\n%s' % (message, '\n'.join(warnings))
        self.env['bus.bus']._sendone(
            self.env.user.partner_id,
            'simple_notification',
            {
                'title': 'Delivery Cost Estimator',
                'message': escape(message),
                'type': 'warning' if warnings else 'success',
                'sticky': bool(warnings),
            },
        )
        _logger.info(
            'DCE %s: origin=%s destination=%s weight=%skg source=%s product=%.3fkg '
            'couriers_requested=%s couriers_quoted=%s warnings=%s',
            self.name,
            origin_area,
            destination_area,
            billable_kg,
            source,
            product_kg,
            ','.join(couriers),
            ','.join(sorted({ln['courier_code'] for ln in lines if ln['courier_code']})),
            len(warnings),
        )
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _dce_estimate_warnings(self, requested_couriers, lines):
        """Return user-facing warnings about things that skew the estimate.

        Biteship answers a multi-courier request with ``success: true`` and only
        the couriers it can actually rate -- on an account without the other
        couriers activated that is a single one. Silently showing one courier
        in a "compare couriers" tab is misleading, so the gap is reported.
        """
        self.ensure_one()
        warnings = []
        quoted = {line['courier_code'] for line in lines if line['courier_code']}
        missing = [code for code in requested_couriers if code not in quoted]
        if missing:
            warnings.append(_(
                'Kurir tanpa tarif untuk rute ini (diabaikan): %s. '
                'Kurir tersebut belum aktif di akun Biteship Anda.'
            ) % ', '.join(sorted(missing)))
        billable_kg, source, product_kg = self._dce_weight_basis()
        if source == 'weight_total' and product_kg and (
            abs(product_kg - billable_kg) > 0.5
        ):
            warnings.append(_(
                'Berat "Weight Total" yang diisi manual (%s kg) berbeda dari '
                'berat produk (%s kg) -- yang dipakai adalah %s kg.'
            ) % (
                _dce_format_kg(billable_kg),
                _dce_format_kg(product_kg),
                _dce_format_kg(billable_kg),
            ))
        return warnings
