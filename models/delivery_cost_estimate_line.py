# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import re

from odoo import _, api, fields, models


class DeliveryCostEstimateLine(models.Model):
    _name = 'delivery.cost.estimate.line'
    _description = 'Delivery Cost Estimate Line'
    _order = 'price asc, id asc'

    picking_id = fields.Many2one(
        'stock.picking',
        string='Delivery Order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Sale Order',
        related='picking_id.sale_id',
        store=True,
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='picking_id.company_id',
        store=True,
        index=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        related='company_id.currency_id',
        store=True,
    )

    courier_code = fields.Char(string='Courier Code')
    courier_name = fields.Char(string='Courier Name')
    service_code = fields.Char(string='Service Code')
    service_name = fields.Char(string='Service Name')
    price = fields.Monetary(string='Estimated Cost', currency_field='currency_id')
    duration_text = fields.Char(string='Duration')

    is_selected = fields.Boolean(
        string='Selected',
        compute='_compute_is_selected',
    )
    is_cheapest = fields.Boolean(
        string='Cheapest',
        compute='_compute_price_flags',
    )
    is_fastest = fields.Boolean(
        string='Fastest',
        compute='_compute_price_flags',
    )
    flag_summary = fields.Char(
        string='Label',
        compute='_compute_price_flags',
    )

    @api.depends('picking_id.selected_delivery_estimate_id')
    def _compute_is_selected(self):
        for line in self:
            selected = line.picking_id.selected_delivery_estimate_id
            line.is_selected = bool(selected) and selected.id == line.id

    @api.depends(
        'price',
        'duration_text',
        'picking_id.delivery_cost_estimate_ids.price',
        'picking_id.delivery_cost_estimate_ids.duration_text',
    )
    def _compute_price_flags(self):
        for line in self:
            lines = line.picking_id.delivery_cost_estimate_ids
            prices = [other.price for other in lines if other.price is not None]
            durations = [
                self._parse_duration_minutes(other.duration_text)
                for other in lines
            ]
            durations = [value for value in durations if value is not None]
            line.is_cheapest = bool(prices) and line.price == min(prices)
            own_duration = self._parse_duration_minutes(line.duration_text)
            line.is_fastest = bool(
                durations
            ) and own_duration is not None and own_duration == min(durations)
            labels = []
            if line.is_cheapest:
                labels.append('Termurah')
            if line.is_fastest:
                labels.append('Tercepat')
            line.flag_summary = ', '.join(labels)

    @staticmethod
    def _parse_duration_minutes(duration_text):
        """Parse a Biteship duration string ('2-3 days', '4 hours', '1 day')
        into the estimated minimum duration in minutes, or ``None`` when the
        text cannot be parsed."""
        if not duration_text:
            return None
        match = re.search(r'(\d+(?:[.,]\d+)?)\s*([a-zA-Z]+)', str(duration_text))
        if not match:
            return None
        try:
            amount = float(match.group(1).replace(',', '.'))
        except ValueError:
            return None
        unit = match.group(2).lower()
        if unit.startswith('minute'):
            return amount
        if unit.startswith('hour'):
            return amount * 60
        if unit.startswith('day'):
            return amount * 24 * 60
        if unit.startswith('week'):
            return amount * 7 * 24 * 60
        return None

    def action_use_delivery_estimate(self):
        """Mark this line as the selected courier for the delivery order."""
        self.ensure_one()
        picking = self.picking_id
        picking.write({'selected_delivery_estimate_id': self.id})
        service = self.service_name or self.courier_code or self.courier_name
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': _('Kurir %s (%s) dipilih untuk pengiriman ini.') % (
                    self.courier_name or self.courier_code,
                    service,
                ),
                'sticky': False,
            },
        }