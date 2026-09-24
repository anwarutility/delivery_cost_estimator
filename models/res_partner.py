# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    biteship_area_id = fields.Char(
        string='Biteship Area ID',
        help='Cached Biteship area_id resolved from the partner shipping '
        'address, to avoid a maps lookup on every cost estimation call.',
    )