# Delivery Cost Estimator

Modul Odoo 16 — perbandingan **estimasi biaya ongkir multi-kurir** (Biteship)
di dalam **Delivery Tracking Dashboard** (`stock_picking_delivery_tracking`).
Tab baru **"Delivery Cost"** memungkinkan admin delivery melihat penawaran dari
semua kurir aktif untuk sebuah Delivery Order, memilih kurir termurah/tercepat,
dan menyimpan pilihannya — tanpa membuat shipment (booking tetap di modul
tracking / manual).

- **Versi Odoo:** 16.0 (Community)
- **Dependensi:** `stock_picking_delivery_tracking`, `stock`, `delivery`,
  `sale`, `mail`
- **Cakupan:** estimasi/perbandingan **hanya**. Tidak membuat order kirim ke
  Biteship, tidak nyambung ke invoicing (`delivery.carrier`), dan **tidak ada
  notifikasi Telegram** dari fitur ini.

## Cara Kerja

1. Buka form Delivery Order (DO) dari Delivery Tracking Dashboard, pindah ke
   tab **"Delivery Cost"**, klik **Hitung Estimasi Ongkir**.
2. Sistem:
   - resolve origin `area_id` (single origin — config, fallback alamat
     gudang/company),
   - resolve destination `area_id` (dicache di `res.partner.biteship_area_id`),
   - hitung berat & dimensi dari move lines (dimensi fallback dari config),
   - panggil `POST /v1/rates/couriers` → tampilkan tabel diurutkan termurah,
   - beri label **Termurah** / **Tercepat**.
3. Klik **Gunakan Kurir Ini** pada satu baris → pilihan tersimpan sebagai
   `selected_delivery_estimate_id` dan terlihat lagi setelah reload.

API hanya dipanggil saat tombol ditekan (explicit action), bukan saat view
dibuka.

## Setup

### 1. API key (reuse dari Delivery Tracking)

Modul ini **tidak** punya field API key sendiri. Key dibaca dari record
provider **"Biteship"** (`delivery.tracking.provider`), yang sama pula dipakai
`stock_picking_delivery_tracking`. Update di **Settings → Delivery Tracking →
API Key** (atau langsung di record provider). Prefix `biteship_live.` untuk
production, `biteship_test.` untuk sandbox. Key tidak pernah dicetak ke log UI
kode.

> Sandbox: akun test `GET /v1/couriers` bisa kosong — isi `active_couriers`
> manual (lihat bawah) untuk mencoba rates.

### 2. Kurir aktif

**Settings → Delivery Cost Estimator → Kode Kurir (koma)**, mis.
`jne,jnt,sicepat,anteraja,ninja,lion,pos,gojek,grab`. Klik **"Ambil Daftar
Kurir"** untuk mengisi otomatis dari Biteship (di-cache per `cache_ttl_hours`,
default 24 jam).

### 3. Origin (single origin)

**Origin Area ID**: kosongkan agar di-resolve otomatis dari alamat
gudang/company saat estimasi pertama, lalu tersimpan ke config. Untuk
multi-gudang, ganti nilai ini (v1 hanya satu origin global).

### 4. Dimensi default

**Dimensi Barang Default (cm)**, format `PxLxT` (default `10x10x10`), dipakai
jika produk tidak memiliki dimensi. Produk akan memakai dimensinya sendiri jika
kolom `length/width/height` ada dan terisi.

### 5. Validasi alamat

Tujuan (partner) wajib memiliki **jalan** dan **kota** (zip/state opsional).
Jika tidak, muncul pesan jelas sebelum API dipanggil.

## Model

- `delivery.cost.estimate.line` — satu baris penawaran (kurir, layanan, harga,
  durasi; flag `is_selected`/`is_cheapest`/`is_fastest` computed, plus
  `flag_summary` label).
- `stock.picking.delivery_cost_estimate_ids` (O2M),
  `selected_delivery_estimate_id` (M2O), `delivery_cost_estimated_at`.
- `res.partner.biteship_area_id` (Char) — cache area tujuan.

## Testing

Suite otomatis (`tests/`, semua HTTP di-mock — 37 test):

```bash
odoo-bin -d agi_test --stop-after-init --test-enable \
  --test-tags=/delivery_cost_estimator -u delivery_cost_estimator
```

Regression (ikutkan modul tracking, 84 test lulus):

```bash
odoo-bin -d agi_test --stop-after-init --test-enable \
  --test-tags=/delivery_cost_estimator,/stock_picking_delivery_tracking \
  -u delivery_cost_estimator,stock_picking_delivery_tracking
```

Cakupan: parsing respons `pricing`, flags termurah/tercepat (termasuk tie),
persistensi pilihan, cache area & kurir (TTL), item/berat/dimensi (min-weight
guard), mock kegagalan API (timeout, 4xx, 5xx) dan **anti-bocor API key**
(401/400 → key tidak muncul di pesan error).

## Batasan (v1)

- **Single origin** global (1 gudang pusat) — extend config per-warehouse hanya
  bila dibutuhkan.
- Tidak ada auto-recalc saat data berubah; hitung manual via tombol.
- Kurir aktif di **sandbox** Biteship bisa kosong (lima akun test) — ini bukan
  kesalahan kode.
- `sale_order_id` pada line mengikuti `picking.sale_id` (traceability); DO
  tanpa SO tetap bisa dihitung (value dari `list_price` produk).