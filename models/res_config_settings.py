# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import _, fields, models
from odoo.exceptions import UserError

from odoo.addons.delivery_cost_estimator.models.stock_picking import (
    DCE_ACTIVE_COURIERS,
    DCE_CACHE_TTL,
    DCE_DEFAULT_DIMS,
    DCE_ORIGIN_AREA,
)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    dce_origin_area_id = fields.Char(
        string='Origin Area ID (asli pengiriman)',
        default='',
        config_parameter=DCE_ORIGIN_AREA,
        help='Biteship area_id untuk alamat asal (single origin untuk v1). '
        'Kosongkan agar di-resolve otomatis dari alamat gudang/company '
        'saat estimasi pertama dihitung.',
    )
    dce_active_couriers = fields.Char(
        string='Kurir Aktif (dibandingkan)',
        default='',
        config_parameter=DCE_ACTIVE_COURIERS,
        help='Kode kurir dipisah koma, mis. jne,jnt,sicepat,anteraja,ninja,'
        'lion,pos,gojek,grab. Gunakan tombol "Ambil Daftar Kurir" atau isi manual.',
    )
    dce_default_item_dimensions = fields.Char(
        string='Dimensi Barang Default (cm)',
        default='10x10x10',
        config_parameter=DCE_DEFAULT_DIMS,
        help='Fallback panjang x lebar x tinggi (cm) saat produk tidak '
        'memiliki dimensi. Format: PxLxT, mis. 20x15x10.',
    )
    dce_cache_ttl_hours = fields.Float(
        string='Cache Kurir / Area (jam)',
        default=24,
        config_parameter=DCE_CACHE_TTL,
        help='Berapa jam daftar kurir aktif & area_id dianggap masih valid '
        'sebelum di-query ulang ke Biteship.',
    )

    def action_dce_refresh_couriers(self):
        """Fetch active couriers from Biteship and fill the config field."""
        client = self.env['stock.picking']._dce_client()
        if not client:
            raise UserError(
                _('Provider Biteship belum dikonfigurasi. '
                  'Cek pengaturan "Delivery Tracking" di atas.')
            )
        couriers = client.get_couriers()
        active = sorted({
            courier.get('courier_code') or courier.get('code')
            for courier in couriers
            if courier.get('courier_code') or courier.get('code')
        })
        if not active:
            raise UserError(
                _('Biteship tidak mengembalikan daftar kurir aktif. '
                  'Periksa API key (sandbox/live) yang terpasang.')
            )
        self.dce_active_couriers = ','.join(active)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': _('%d kurir aktif ditampilkan di form. '
                             'Klik "Simpan" agar tersimpan.') % len(active),
                'sticky': False,
            },
        }