# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

{
    'name': 'Delivery Cost Estimator',
    'version': '16.0.0.7',
    'summary': 'Biteship multi-courier delivery cost comparison for Delivery Orders',
    'category': 'Inventory',
    'author': 'PT Aston Graphindo Indonesia',
    'website': 'https://www.astongraphindo.com',
    'depends': [
        'stock_picking_delivery_tracking',
        'stock',
        'delivery',
        'sale',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/stock_picking_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}