# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

"""Biteship rate-estimation client.

Reuses the provider/auth plumbing of ``stock_picking_delivery_tracking`` (the
``TrackingAPIClient`` base class) so credentials stay in one place — the
``delivery.tracking.provider`` record — and adds the two endpoints needed by
this module:

* ``POST /v1/rates/couriers``  -> cost comparison
* ``GET  /v1/maps/areas``      -> area_id resolution (origin & destination)

Like the base class this client is pure Python (no Odoo dependency), never
raises on API/network failures (failures are returned as structured error
results) and never logs the API key.
"""

import logging
import time

import requests

from odoo.addons.stock_picking_delivery_tracking.services.tracking_api_client import (
    TrackingAPIClient,
)

_logger = logging.getLogger(__name__)


class BiteshipRatesClient(TrackingAPIClient):
    """Biteship client extended with rate-estimation and area-search calls."""

    def _error_result(self, message, raw_response=None, error_code=None):
        """Structured error result shared by this module's API calls."""
        return {
            "success": False,
            "pricing": [],
            "error": message,
            "error_code": error_code or "http_error",
            "raw_response": raw_response,
        }

    def get_rates(self, origin_area_id, destination_area_id, couriers, items):
        """Call ``POST /v1/rates/couriers`` and return a result dict.

        On success the dict contains ``success=True`` and ``pricing`` (the raw
        Biteship pricing array). Every failure path returns a dict with
        ``success=False``, an empty ``pricing`` list and a user-safe ``error``
        message. Never raises.
        """
        if not origin_area_id or not destination_area_id:
            return self._error_result(
                "missing origin or destination area id",
                error_code="invalid_arguments",
            )
        if not couriers:
            return self._error_result(
                "no active couriers configured",
                error_code="invalid_arguments",
            )
        payload = {
            "origin_area_id": origin_area_id,
            "destination_area_id": destination_area_id,
            "couriers": ",".join(couriers),
            "items": items or [],
        }
        url = "%s/v1/rates/couriers" % self.api_base_url
        headers = self._build_auth_header()
        last_response = None
        for attempt in range(self.retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                _logger.warning(
                    "Biteship get_rates network error (attempt %d/%d): %s",
                    attempt + 1,
                    self.retries + 1,
                    exc,
                )
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                return self._error_result(
                    "koneksi ke Biteship gagal: %s" % exc,
                    error_code="network_error",
                )
            last_response = response
            if response.status_code >= 500:
                _logger.warning(
                    "Biteship get_rates server error HTTP %s (attempt %d/%d): %s",
                    response.status_code,
                    attempt + 1,
                    self.retries + 1,
                    response.text[:500],
                )
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                return self._error_result(
                    "server Biteship bermasalah (HTTP %s)" % response.status_code,
                    raw_response=response.text,
                    error_code="http_5xx",
                )
            if response.status_code in (401, 403):
                _logger.error(
                    "Biteship get_rates authentication failed HTTP %s",
                    response.status_code,
                )
                return self._error_result(
                    "autentikasi API Biteship gagal (HTTP %s)" % response.status_code,
                    raw_response=response.text,
                    error_code="auth_error",
                )
            if response.status_code != 200:
                _logger.warning(
                    "Biteship get_rates unexpected HTTP %s: %s",
                    response.status_code,
                    response.text[:500],
                )
                error_message = "HTTP %s" % response.status_code
                if response.text:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict):
                        detail = body.get("error") or body.get("message")
                        if detail:
                            error_message = "%s: %s" % (error_message, detail)
                return self._error_result(
                    error_message,
                    raw_response=response.text,
                    error_code="http_error",
                )
            break

        try:
            data = last_response.json()
        except ValueError as exc:
            _logger.error("Biteship get_rates invalid JSON: %s", exc)
            return self._error_result(
                "response Biteship tidak valid",
                raw_response=last_response.text,
                error_code="parse_error",
            )
        if not data.get("success"):
            message = (
                data.get("message")
                or data.get("error")
                or "no pricing for this request"
            )
            _logger.warning("Biteship get_rates success=false: %s", message)
            return self._error_result(
                "estimasi tidak tersedia: %s" % message,
                raw_response=data,
                error_code="api_error",
            )
        return {
            "success": True,
            "pricing": data.get("pricing") or [],
            "raw_response": data,
        }

    def search_areas(self, query, postal_code=None, limit=5, countries="ID"):
        """Search Biteship areas with ``GET /v1/maps/areas``.

        Returns a list of matching area dicts (``id``, ``name``,
        ``postal_code``). Never raises: failures log and return ``[]``.
        """
        if not query:
            return []
        params = {"countries": countries, "input": query, "limit": limit}
        url = "%s/v1/maps/areas" % self.api_base_url
        try:
            response = requests.get(
                url,
                headers=self._build_auth_header(),
                params=params,
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            _logger.error("Biteship search_areas network error: %s", exc)
            return []
        if response.status_code != 200:
            _logger.warning(
                "Biteship search_areas HTTP %s: %s",
                response.status_code,
                response.text[:500],
            )
            return []
        try:
            data = response.json()
        except ValueError as exc:
            _logger.error("Biteship search_areas invalid JSON: %s", exc)
            return []
        return data.get("areas") or []