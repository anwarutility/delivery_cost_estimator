# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

"""Delivery Order side of the Biteship estimate.

All provider plumbing, area resolution, weight basis and pricing parsing live
in ``delivery.cost.estimate.mixin`` so the Sales Order button quotes the exact
same route. This model only says *what the parcel is* for a picking: the moves
that ship, and the two address partners.
"""

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from odoo.addons.delivery_cost_estimator.models.delivery_cost_estimate_mixin import (
    DeliveryCostEstimateMixin,
    dce_format_kg,
)

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _name = 'stock.picking'
    _inherit = ['stock.picking', 'delivery.cost.estimate.mixin']

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
    # Mixin hooks: what the parcel is, and where it goes
    # ---------------------------------------------------------------------

    def _dce_origin_partner(self):
        """Single origin: the company's first warehouse, else the company."""
        self.ensure_one()
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.company_id.id)],
            limit=1,
        )
        if warehouse and warehouse.partner_id:
            return warehouse.partner_id
        return self.company_id.partner_id

    def _dce_destination_partner(self):
        return self.partner_id

    def _dce_reference(self):
        return self.name

    def _dce_weight_moves(self):
        """Moves whose weight counts towards the quoted parcel.

        ``move_ids_without_package`` (the same set ``raja_ongkir`` uses) keeps
        the two tabs of one Delivery Order from disagreeing, and cancelled
        moves are dropped: a line Odoo already released no longer ships, yet it
        was still adding weight (0.35 kg of a cancelled mouse on DO 3015).
        """
        moves = self.move_ids_without_package or self.move_ids
        return moves.filtered(lambda move: move.state != 'cancel')

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
        _pricing, lines, couriers = self._dce_collect_rates()
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
            dce_format_kg(billable_kg),
        )
        if warnings:
            message = '%s\n\n%s' % (message, '\n'.join(warnings))
        self.env['bus.bus']._sendone(
            self.env.user.partner_id,
            'simple_notification',
            {
                'title': 'Delivery Cost Estimator',
                'message': message,
                'type': 'warning' if warnings else 'success',
                'sticky': bool(warnings),
            },
        )
        _logger.info(
            'DCE %s: weight=%skg source=%s product=%.3fkg warnings=%s',
            self.name,
            billable_kg,
            source,
            product_kg,
            len(warnings),
        )
        return {'type': 'ir.actions.client', 'tag': 'reload'}
