"""
Dataverse admin workflow IP-whitelist helper.

Extracted from the legacy `_ensure_workflow_resume_ip_allowed` block.
Mostly the same logic; lifted to a service so it can be tested in
isolation. Still gated by `DATAVERSE_AUTO_WHITELIST_WORKFLOW_RESUME=1`
(safe-default off per Q3 / Q5 hardening posture).
"""

import logging
import os
import socket
from typing import Optional
from urllib.parse import urlparse

import requests

from apps.dataverse.services.client import DataverseClient

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _outbound_ip_for_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return None

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            return sock.getsockname()[0]
    except OSError as exc:
        logger.warning("Could not resolve outbound IP for %s: %s", url, exc)
        return None


def _whitelist_value(response: requests.Response) -> str:
    try:
        data = response.json().get("data")
    except ValueError:
        return response.text.strip()

    if isinstance(data, dict):
        return str(data.get("message") or data.get("value") or "").strip()
    if isinstance(data, str):
        return data.strip()
    return ""


def ensure_workflow_resume_ip_allowed(client: DataverseClient, resume_base_url: str) -> None:
    """Add the catalog's outbound IP to the Dataverse workflow whitelist if
    `DATAVERSE_AUTO_WHITELIST_WORKFLOW_RESUME=1`.

    No-op otherwise. The legacy default was `1`; this proposal flips it
    to `0` so the privilege-escalation primitive is operator-opt-in.
    """
    if not _env_flag("DATAVERSE_AUTO_WHITELIST_WORKFLOW_RESUME", default=False):
        return

    if not client.token:
        logger.warning("Cannot update workflow whitelist without DATAVERSE_API_TOKEN")
        return

    outbound_ip = _outbound_ip_for_url(resume_base_url)
    if not outbound_ip:
        return

    try:
        response = client.get_workflow_whitelist()
        if response.status_code != 200:
            logger.warning(
                "Could not read Dataverse workflow whitelist: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return

        current = _whitelist_value(response)
        addresses = [address.strip() for address in current.split(";") if address.strip()]
        if outbound_ip in addresses:
            return

        addresses.append(outbound_ip)
        updated = ";".join(addresses)
        update_response = client.put_workflow_whitelist(updated)
        if update_response.status_code not in range(200, 300):
            logger.warning(
                "Could not update Dataverse workflow whitelist: status=%s body=%s",
                update_response.status_code,
                update_response.text[:500],
            )
            return

        logger.info("Added %s to Dataverse workflow resume whitelist", outbound_ip)
    except requests.RequestException as exc:
        logger.warning("Workflow whitelist update failed: %s", exc, exc_info=True)
