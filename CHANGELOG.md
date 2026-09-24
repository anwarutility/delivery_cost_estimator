# Changelog - Delivery Cost Estimator

Module: `delivery_cost_estimator` — perbandingan biaya ongkir multi-kurir
(Biteship) di dalam Delivery Tracking Dashboard (`stock_picking_delivery_tracking`).

## v16.0.0.6 — 2026-09-24: Hotfix (Pilihan kurir & biaya tidak muncul di form)

### Perbaikan
- Setelah klik **"Hitung Estimasi Ongkir"** hasilnya tersimpan ke `stock.picking`
  dan notifikasi sukses muncul, tetapi **daftar penawaran tidak tampil** di tab
  "Delivery Cost" karena tombol hanya mengembalikan `display_notification` dan
  form tidak di-reload (O2M tidak di-refresh). Sekarang:
  - toast sukses dikirim via `bus.bus._sendone` (`simple_notification`), dan
  - action mengembalikan `{'type': 'ir.actions.client', 'tag': 'reload'}`
    sehingga form langsung menampilkan baris kurir/layanan/biaya & penanda
    pilihan (juga berlaku untuk "Gunakan Kurir Ini").
- `_get_active_couriers()` kini **dedupe tanpa urutan berubah** — daftar
  `active_couriers` sebelumnya menyimpan duplikat (mis. `gojek,gojek,...`
  karena Biteship mengembalikan satu baris per service). Parameter yang sudah
  terlanjur berisi duplikat dibersihkan di instalasi ini.
- Diverifikasi live di `agi` (DO 2975): aksi sukses, `reload` kembali, 2 baris
  ter-tulis, `estimated_at` ter-update, pilihan ter-reset.

## v16.0.0.5 — 2026-09-24: Hotfix (Tombol "Ambil Daftar Kurir")

### Perbaikan
- Tombol **"Ambil Daftar Kurir"** di Settings selalu memunculkan
  `Biteship tidak mengembalikan daftar kurir aktif` meskipun API key valid.
  Penyebab: `GET /v1/couriers` mengembalikan field **`courier_code`**, bukan
  `code` — pembacaan di `res.config.settings.action_dce_refresh_couriers()` dan
  `stock.picking._get_active_couriers()` hanya membaca `code`, sehingga daftar
  selalu kosong saat cache kedaluwarsa. (Di sandbox masalah tak terlihat karena
  akun test memang mengembalikan `[]`.)
- Sekarang membaca `courier_code` dengan fallback `code` (defensif) di kedua
  jalur. Diverifikasi live di `agi` dengan key live: 22 kurir aktif terbaca.
- Test baru: `test_settings_refresh_couriers_fills_config` (payload
  `courier_code` → settings terisi) dan
  `test_active_couriers_code_field_fallback`; fixture refetch diubah ke shape
  API asli. Suite: **39 test lulus**.

## v16.0.0.4 — 2026-09-24: CP4 (Integrasi & Testing)

### Perbaikan / Penyempurnaan
- Validasi alamat tujuan **sebelum** panggil API: `_dce_missing_address_parts()`
  memerlukan `street/street2` + `city` (zip/state opsional). Pesan error jelas:
  `Alamat tujuan (customer) belum lengkap: Jalan, Kota. ...`, tanpa memanggil
  Biteship.
- E2E live dengan key sandbox terverifikasi: resolve origin
  (`IDNP10IDNC163IDND1132` → tersimpan ke config) & destination
  (`IDNP6IDNC147IDND835` → ter-cache di `res.partner`), rates pipeline
  menghasilkan baris (jne Reguler Rp 28.000 "Termurah, Tercepat", jne JNE
  Trucking Rp 155.000), seleksi kurir tersimpan.
- Keamanan: audit log memastikan API key tidak pernah tercetak di log error,
  pesan UserError, atau chatter (0 match pola JWT di seluruh log E2E/test).
- Regression: test `stock_picking_delivery_tracking` tetap lulus
  (dashboard/tab Riwayat Tracking tidak terganggu).

## v16.0.0.3 — 2026-09-24: CP3 (UI/UX)

### Fitur
- Tab **"Delivery Cost"** pada form `stock.picking` (pola sama dengan tab
  "Riwayat Tracking"): tombol **"Hitung Estimasi Ongkir"**, daftar penawaran
  (Kurir, Layanan, Estimasi Biaya, Durasi, label "Termurah"/"Tercepat",
  penanda pilihan) diurutkan termurah ke atas, dan tombol per-baris
  **"Gunakan Kurir Ini"**.
- Backend action:
  - `stock.picking.action_estimate_delivery_cost()` — validasi berurutan
    (non-outgoing, provider belum dikonfigurasi, origin tak ter-resolve, alamat
    tujuan tak lengkap, tidak ada baris produk, daftar kurir kosong), memanggil
    `POST /v1/rates/couriers`, menulis/ mengganti baris O2M, mengosongkan
    pilihan, mencatat `delivery_cost_estimated_at`, notifikasi ringkasan.
  - `delivery.cost.estimate.line.action_use_delivery_estimate()` — menyimpan
    pilihan ke `selected_delivery_estimate_id` + notifikasi.
- Field computed `flag_summary` (label gabungan "Termurah, Tercepat") pada line.
- Halaman **Settings > Delivery Cost Estimator**: Origin Area ID (single
  origin), Kode Kurir aktif + tombol **"Ambil Daftar Kurir"**, dimensi default
  produk, TTL cache. API key **tidak diduplikasi** — reuse provider
  "Biteship" dari `stock_picking_delivery_tracking`.

### Catatan
- Kurir aktif di akun **sandbox** Biteship kosong (`GET /v1/couriers` tidak
  mengembalikan apa pun di akun test) — di lingkungan live, tombol "Ambil
  Daftar Kurir" mengisi otomatis.

## v16.0.0.2 — 2026-09-24: CP2 (Biteship API Client)

### Fitur
- `services/biteship_client.py`: `BiteshipRatesClient` — subclass
  `TrackingAPIClient` (auth/header/provider di-reuse, single source of truth):
  - `get_rates()` → `POST /v1/rates/couriers`, retry timeout/5xx, mapping
    401/403→auth, 4xx→http_error (detail body dipertahankan), `success=false`
    → api_error. `never raises`.
  - `search_areas()` → `GET /v1/maps/areas` (negara ID).
- Model helpers `stock.picking`:
  - `_resolve_origin_area_id()` — single origin: config `origin_area_id`, jika
    kosong resolve dari alamat gudang/company lalu persist ke config.
  - `_get_delivery_destination_area_id()` — cache `res.partner.biteship_area_id`.
  - `_get_active_couriers()` — cache daftar kurir dgn TTL
    (`cache_ttl_hours`, default 24).
  - `_build_delivery_items()` — item per produk; berat kg→gram; dimensi fallback
    dari config (produk clone `agi` tidak punya kolom L/W/H); guard berat min 1g.
- Config `ir.config_parameter`: `delivery_cost_estimator.origin_area_id`,
  `.active_couriers`, `.active_couriers_updated`, `.default_item_dimensions`,
  `.cache_ttl_hours`.

## v16.0.0.1 — 2026-09-24: CP1 (Scaffolding)

### Fitur
- Struktur modul (manifest depends `stock_picking_delivery_tracking`, `stock`,
  `delivery`, `sale`, `mail`; security grouped ke `stock.group_stock_user`/
  `stock.group_stock_manager`).
- Model `delivery.cost.estimate.line` (semua field PRD; `is_selected` computed
  dari `picking.selected_delivery_estimate_id`; `is_cheapest`/`is_fastest`
  computed + `_parse_duration_minutes`).
- Extend `stock.picking` (`delivery_cost_estimate_ids`,
  `selected_delivery_estimate_id`, `delivery_cost_estimated_at`) dan
  `res.partner` (`biteship_area_id`).

### Keamanan / Operasional
- Key sandbox Biteship dipasang ke provider "Biteship"; cron "Delivery Tracking
  Status Poll" & "PR Telegram" di-pause — **dengan `env.cr.commit()` eksplisit**
  (pelajaran di CP0: `odoo shell` tidak auto-commit) dan diverifikasi via
  `psql` terpisah.