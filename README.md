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

## Lingkungan & Database

Modul ini dipasang di dua instansi Odoo dengan database yang berbeda:

| Server/Instansi | Database Yang Dipakai |
|---|---|
| **AGI** (Production PT Aston Graphindo Indonesia) | `agi` |
| **ASN** | `based` |

> Saat update/upgrade modul, jalankan pada database sesuai instansi:
> `-d agi` di server AGI, atau `-d based` di server ASN.

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
3. Jika alamat tujuan tidak bisa dipetakan **pasti** ke satu area Biteship,
   proses dihentikan dengan pesan jelas — sistem tidak pernah menebak zona dari
   pencocokan kabur.
4. Klik **Gunakan Kurir Ini** pada satu baris → pilihan tersimpan sebagai
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

Tujuan (partner) wajib memiliki **jalan** dan **kota**. Agar zona ongkir tidak
salah, sertakan juga salah satu dari:

- **kecamatan** — diambil dari field terstruktur `subdistrict_id` (diisi modul
  `raja_ongkir`) bila ada, lalu fallback ke teks `Kec. ...` di alamat; atau
- **kode pos** (`zip`).

> Kecamatan terstruktur lebih diutamakan daripada parsing alamat jalan: alamat
> yang menulis "Sumbersari" sementara kecamatannya "Sumber Sari" membuat
> pencarian Biteship tidak menemukan area sama sekali.

Kalau keduanya tidak ada dan hanya kota yang cocok, estimasi **dihentikan** dengan
pesan yang menjelaskan cara melengkapinya — dulu sistem diam-diam memakai
`areas[0]` dari pencarian kabur Biteship sehingga bisa quoted ke kabupaten yang
salah.

> Satu kode pos sering menutupi beberapa kecamatan (mis. `57731` = Jaten **dan**
> Ngargoyoso). Karena itu nama kecamatan lebih diprioritaskan daripada kode pos,
> dan kode pos yang cocok dengan lebih dari satu area results tetap dianggap
> ambigu.

### 6. Berat kirim

Berat yang dikirim ke Biteship ditentukan berurutan:

1. `stock.picking.weight_total` bila modul `raja_ongkir` terpasang dan nilainya
   diisi (>0) — berat manual parcels, **mengalahkan** hitungan produk;
2. jika tidak, jumlah `product.weight * qty` dari move lines, dibulatkan ke atas
   ke kilogram penuh (`ceil`), minimal 1 kg.

Move yang sudah di-cancel tidak dihitung, dan yang sudah masuk paket tidak
terhitung dua kali — aturannya sama persis dengan tab RajaOngkir.
3. qty per baris memakai `quantity_done` bila sudah diproses, jika belum
   memakai `product_uom_qty` yang direservasi.

Ketika `weight_total` diisi, bobot per baris item diskalakan proporsional; produk
tanpa bobot diletakkan pada baris terbanyak. Pembulatan selalu ke **bawah**
supaya total tidak pernah melewati batas kilogram (Biteship membulatkan paket ke
kilogram penuh, jadi 1 gram lebih dari 2 kg akan meloncat ke tingkat harga 3 kg).

## Model

- `delivery.cost.estimate.line` — satu baris penawaran (kurir, layanan, harga,
  durasi; flag `is_selected`/`is_cheapest`/`is_fastest` computed, plus
  `flag_summary` label).
- `delivery.cost.estimate.mixin` — mesin bersama (provider/auth, resolusi area,
  kurir aktif, berat/dimensi, parsing harga, peringatan) yang dipakai
  `stock.picking`; memisahkannya dari model konkret membuat logika ini bisa
  dipakai ulang tanpa menyalin.
- `stock.picking.delivery_cost_estimate_ids` (O2M),
  `selected_delivery_estimate_id` (M2O), `delivery_cost_estimated_at`.
- `res.partner.biteship_area_id` (Char) — cache area tujuan.

## Testing

Suite otomatis (`tests/`, semua HTTP di-mock — 67 test):

```bash
odoo-bin -d agi_test --stop-after-init --test-enable \
  --test-tags=/delivery_cost_estimator -u delivery_cost_estimator
```

Regression (ikutkan modul tracking, 106 test lulus):

```bash
odoo-bin -d agi_test --stop-after-init --test-enable \
  --test-tags=/delivery_cost_estimator,/stock_picking_delivery_tracking \
  -u delivery_cost_estimator,stock_picking_delivery_tracking
```

> Jalankan dari `/tmp`; user `odoo` tidak bisa membaca `/home/oem`.

Cakupan: parsing respons `pricing`, flags termurah/tercepat (termasuk tie),
persistensi pilihan, cache area & kurir (TTL), item/berat/dimensi (min-weight
guard, `weight_total` manual, pembulatan ke bawah), pemilihan area (kecamatan
lebih diprioritaskan, kode pos bersama ditolak, tebakan kabur ditolak), mock
kegagalan API (timeout, 4xx, 5xx) dan **anti-bocor API key**
(401/400 → key tidak muncul di pesan error).

## Batasan (v1)

- **Single origin** global (1 gudang pusat) — extend config per-warehouse hanya
  bila dibutuhkan.
- Tidak ada auto-recalc saat data berubah; hitung manual via tombol.
- Kurir aktif di **sandbox** Biteship bisa kosong (lima akun test) — ini bukan
  kesalahan kode.
- `sale_order_id` pada line mengikuti `picking.sale_id` (traceability); DO
  tanpa SO tetap bisa dihitung (value dari `list_price` produk).