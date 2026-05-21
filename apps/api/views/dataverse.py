import json
import logging
import os
import socket
import threading
import time
from http import HTTPStatus
from urllib.parse import urlparse

from django.db import transaction

import requests
from django.http import HttpResponse
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from apps import openapi
from apps.core.errors import ProblemDetailException
from apps.core.models import Entry, Acquisition, Catalog, User, Author, EntryAuthor, Language
from partial_date import PartialDate

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _outbound_ip_for_url(url: str) -> str | None:
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
    except OSError as e:
        logger.warning(f"[DATAVERSE-PREPUBLISH] Could not resolve outbound IP for {url}: {e}")
        return None


def _workflow_whitelist_value(response: requests.Response) -> str:
    try:
        data = response.json().get("data")
    except ValueError:
        return response.text.strip()

    if isinstance(data, dict):
        return str(data.get("message") or data.get("value") or "").strip()
    if isinstance(data, str):
        return data.strip()
    return ""


def _ensure_workflow_resume_ip_allowed(dataverse_base_url: str, dataverse_token: str, resume_base_url: str) -> None:
    if not _env_flag("DATAVERSE_AUTO_WHITELIST_WORKFLOW_RESUME", default=False):
        return

    if not dataverse_token:
        logger.warning("[DATAVERSE-PREPUBLISH] Cannot update workflow whitelist without DATAVERSE_API_TOKEN")
        return

    outbound_ip = _outbound_ip_for_url(resume_base_url)
    if not outbound_ip:
        return

    whitelist_url = f"{dataverse_base_url}/api/admin/workflows/ip-whitelist"
    headers = {"X-Dataverse-key": dataverse_token}

    try:
        response = requests.get(whitelist_url, headers=headers, timeout=30)
        if response.status_code != 200:
            logger.warning(
                "[DATAVERSE-PREPUBLISH] Could not read Dataverse workflow whitelist: "
                f"status={response.status_code} body={response.text[:500]}"
            )
            return

        current = _workflow_whitelist_value(response)
        addresses = [address.strip() for address in current.split(";") if address.strip()]
        if outbound_ip in addresses:
            return

        addresses.append(outbound_ip)
        updated = ";".join(addresses)
        update_response = requests.put(
            whitelist_url,
            data=updated,
            headers={**headers, "Content-Type": "text/plain; charset=utf-8"},
            timeout=30,
        )
        if update_response.status_code not in range(200, 300):
            logger.warning(
                "[DATAVERSE-PREPUBLISH] Could not update Dataverse workflow whitelist: "
                f"status={update_response.status_code} body={update_response.text[:500]}"
            )
            return

        logger.info(f"[DATAVERSE-PREPUBLISH] Added {outbound_ip} to Dataverse workflow resume whitelist")
    except requests.RequestException as e:
        logger.warning(f"[DATAVERSE-PREPUBLISH] Workflow whitelist update failed: {e}", exc_info=True)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _resume_dataverse_workflow(
    dataverse_base_url: str,
    dataverse_token: str,
    invocation_id: str,
    *,
    max_attempts: int,
    initial_delay: float,
) -> None:
    resume_base_url = os.getenv("DV_WORKFLOW_RESUME_BASE", dataverse_base_url).rstrip("/")
    _ensure_workflow_resume_ip_allowed(dataverse_base_url, dataverse_token, resume_base_url)

    resume_url = f"{resume_base_url}/api/workflows/{invocation_id}"
    delay = initial_delay

    for attempt in range(1, max_attempts + 1):
        if delay > 0:
            time.sleep(delay)

        logger.info(
            "[DATAVERSE-PREPUBLISH] Resuming Dataverse workflow invocation "
            f"{invocation_id} (attempt {attempt}/{max_attempts})"
        )

        try:
            response = requests.post(
                resume_url,
                data="OK",
                headers={"Content-Type": "text/plain; charset=utf-8"},
                timeout=60,
            )
        except requests.RequestException as e:
            logger.warning(
                f"[DATAVERSE-PREPUBLISH] Dataverse workflow resume request failed on attempt {attempt}: {e}",
                exc_info=True,
            )
        else:
            if response.status_code in range(200, 300):
                logger.info(
                    "[DATAVERSE-PREPUBLISH] Dataverse workflow resume accepted: "
                    f"status={response.status_code} body={response.text[:500]}"
                )
                return

            if response.status_code == 404 and attempt < max_attempts:
                logger.info(
                    "[DATAVERSE-PREPUBLISH] Dataverse workflow invocation is not ready yet: "
                    f"status=404 body={response.text[:500]}"
                )
            else:
                logger.warning(
                    "[DATAVERSE-PREPUBLISH] Dataverse workflow resume returned an error: "
                    f"status={response.status_code} body={response.text[:1000]}"
                )

        delay = min(delay * 2 if delay else 1, 30)

    logger.error(f"[DATAVERSE-PREPUBLISH] Dataverse workflow resume gave up for invocation {invocation_id}")


def _schedule_dataverse_workflow_resume(
    dataverse_base_url: str,
    dataverse_token: str,
    invocation_id: str | None,
) -> None:
    if not _env_flag("DATAVERSE_RESUME_WORKFLOW", default=False):
        return

    if not invocation_id:
        logger.warning("[DATAVERSE-PREPUBLISH] DATAVERSE_RESUME_WORKFLOW is enabled but invocation_id is missing")
        return

    max_attempts = _int_env("DATAVERSE_RESUME_WORKFLOW_ATTEMPTS", 10)
    initial_delay = _float_env("DATAVERSE_RESUME_WORKFLOW_INITIAL_DELAY", 1)

    thread = threading.Thread(
        target=_resume_dataverse_workflow,
        kwargs={
            "dataverse_base_url": dataverse_base_url,
            "dataverse_token": dataverse_token,
            "invocation_id": invocation_id,
            "max_attempts": max_attempts,
            "initial_delay": initial_delay,
        },
        name=f"dataverse-workflow-resume-{invocation_id}",
        daemon=True,
    )
    thread.start()
    logger.info(f"[DATAVERSE-PREPUBLISH] Scheduled Dataverse workflow resume for invocation {invocation_id}")


@method_decorator(csrf_exempt, name="dispatch")
class DataverseSync(View):
    @openapi.metadata(description="Dataverse workflow post-publish sync", tags=["Dataverse"])
    def post(self, request):
        try:
            raw = request.body.decode("utf-8") if request.body else "{}"
            payload = json.loads(raw)
        except Exception as e:
            raise ProblemDetailException(_("Invalid JSON payload"), status=HTTPStatus.BAD_REQUEST, previous=e)

        dataset_id = payload.get("dataset_id")
        global_id = payload.get("global_id")
        title = payload.get("title")

        if dataset_id is None or global_id is None:
            raise ProblemDetailException(
                _("Missing required fields: dataset_id and global_id"),
                status=HTTPStatus.BAD_REQUEST,
            )

        logger.info(
            f"[DATAVERSE-SYNC] received publish event: dataset_id={dataset_id} global_id={global_id} title={title}"
        )

        return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")


@method_decorator(csrf_exempt, name="dispatch")
class DataversePrepublishIngest(View):
    @openapi.metadata(description="Dataverse workflow pre-publish log file URLs", tags=["Dataverse"])
    def post(self, request, invocation_id=None):
        logger.info("[DATAVERSE-PREPUBLISH] ===== Starting request processing =====")
        logger.info(f"[DATAVERSE-PREPUBLISH] Request method: {request.method}")
        logger.info(f"[DATAVERSE-PREPUBLISH] Request headers: {dict(request.headers)}")

        try:
            raw = request.body.decode("utf-8") if request.body else "{}"
            logger.info(f"[DATAVERSE-PREPUBLISH] Raw request body: {raw}")
            payload = json.loads(raw)
            logger.info(f"[DATAVERSE-PREPUBLISH] Parsed payload: {json.dumps(payload, indent=2)}")
        except Exception as e:
            logger.error(f"[DATAVERSE-PREPUBLISH] Failed to parse JSON: {e}", exc_info=True)
            raise ProblemDetailException(_("Invalid JSON payload"), status=HTTPStatus.BAD_REQUEST, previous=e)

        secret = payload.get("secret")
        dataset_id = payload.get("dataset_id")
        global_id = payload.get("global_id")
        title = payload.get("title")
        invocation_id = payload.get("invocation_id") or payload.get("invocationId") or invocation_id

        logger.info(
            "[DATAVERSE-PREPUBLISH] Extracted values - "
            f"dataset_id: {dataset_id}, global_id: {global_id}, title: {title}, invocation_id: {invocation_id}"
        )

        expected_secret = os.getenv("DATAVERSE_WORKFLOW_SECRET", "")
        logger.info(
            f"[DATAVERSE-PREPUBLISH] Checking secret - provided: {'***' if secret else 'None'}, expected: {'***' if expected_secret else 'None'}"
        )
        if not expected_secret or secret != expected_secret:
            logger.warning(f"[DATAVERSE-PREPUBLISH] Secret validation failed")
            raise ProblemDetailException(_("Forbidden"), status=HTTPStatus.FORBIDDEN)

        if dataset_id is None or global_id is None:
            logger.error(
                f"[DATAVERSE-PREPUBLISH] Missing required fields - dataset_id: {dataset_id}, global_id: {global_id}"
            )
            raise ProblemDetailException(
                _("Missing required fields: dataset_id and global_id"),
                status=HTTPStatus.BAD_REQUEST,
            )

        logger.info(f"[DATAVERSE-PREPUBLISH] Validation passed, proceeding with processing")

        dv_base_internal = os.getenv("DV_BASE_INTERNAL", "http://dataverse:8080").rstrip("/")
        dv_public_base = os.getenv("DV_PUBLIC_BASE", dv_base_internal).rstrip("/")
        dv_token = os.getenv("DATAVERSE_API_TOKEN", "").strip()

        logger.info(
            f"[DATAVERSE-PREPUBLISH] Dataverse config - base_internal: {dv_base_internal}, public_base: {dv_public_base}, token: {'***' if dv_token else 'MISSING'}"
        )

        if not dv_token:
            logger.error("[DATAVERSE-PREPUBLISH] DATAVERSE_API_TOKEN environment variable is missing")
            raise ProblemDetailException(_("Missing DATAVERSE_API_TOKEN"), status=HTTPStatus.INTERNAL_SERVER_ERROR)

        # Fetch dataset metadata first
        dataset_url = f"{dv_base_internal}/api/datasets/{dataset_id}/versions/:draft"
        logger.info(f"[DATAVERSE-PREPUBLISH] Fetching dataset metadata from Dataverse: {dataset_url}")

        dataset_metadata = {}
        try:
            dataset_resp = requests.get(dataset_url, headers={"X-Dataverse-key": dv_token}, timeout=60)
            if dataset_resp.status_code == 200:
                dataset_metadata = dataset_resp.json().get("data", {})
                logger.info(f"[DATAVERSE-PREPUBLISH] Dataset metadata keys: {list(dataset_metadata.keys())}")
            else:
                logger.warning(
                    f"[DATAVERSE-PREPUBLISH] Failed to fetch dataset metadata - status: {dataset_resp.status_code}"
                )
        except Exception as e:
            logger.warning(f"[DATAVERSE-PREPUBLISH] Error fetching dataset metadata: {e}", exc_info=True)

        # Fetch files
        files_url = f"{dv_base_internal}/api/datasets/{dataset_id}/versions/:draft/files"
        logger.info(f"[DATAVERSE-PREPUBLISH] Fetching files from Dataverse: {files_url}")

        try:
            resp = requests.get(files_url, headers={"X-Dataverse-key": dv_token}, timeout=60)
            logger.info(
                f"[DATAVERSE-PREPUBLISH] Dataverse API response - status: {resp.status_code}, headers: {dict(resp.headers)}"
            )
        except Exception as e:
            logger.error(f"[DATAVERSE-PREPUBLISH] Failed to connect to Dataverse: {e}", exc_info=True)
            raise ProblemDetailException(
                _("Dataverse connection failed"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=str(e),
            )

        if resp.status_code != 200:
            logger.error(
                f"[DATAVERSE-PREPUBLISH] Dataverse API error - status: {resp.status_code}, body: {resp.text[:500]}"
            )
            raise ProblemDetailException(
                _("Dataverse file listing failed"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=f"status={resp.status_code} body={resp.text[:2000]}",
            )

        try:
            response_json = resp.json()
            logger.info(
                f"[DATAVERSE-PREPUBLISH] Dataverse response JSON keys: {list(response_json.keys()) if isinstance(response_json, dict) else 'Not a dict'}"
            )
            items = (response_json or {}).get("data") or []
        except Exception as e:
            logger.error(f"[DATAVERSE-PREPUBLISH] Failed to parse Dataverse response JSON: {e}", exc_info=True)
            logger.error(f"[DATAVERSE-PREPUBLISH] Response text: {resp.text[:500]}")
            raise ProblemDetailException(
                _("Invalid Dataverse response format"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=str(e),
            )

        logger.info(
            f"[DATAVERSE-PREPUBLISH] Found {len(items)} files in dataset - dataset_id: {dataset_id}, global_id: {global_id}, title: {title}"
        )

        def resolve_catalog() -> Catalog:
            """
            Resolve target catalog using a JSON map (Dataverse → EvilFlowers) with fallbacks.
            Priority:
            1) dataset_id map
            2) global_id prefix map (longest prefix wins)
            3) DATAVERSE_CATALOG_URL_NAME env
            4) map default
            5) first catalog
            """
            logger.info("[DATAVERSE-PREPUBLISH] Resolving catalog using fixed Dataverse -> catalog mapping")
            catalog = Catalog.objects.order_by("id").first()
            if catalog:
                logger.info(
                    f"[DATAVERSE-PREPUBLISH] Using fixed catalog: {catalog.url_name} (id: {catalog.pk}, title: {catalog.title})"
                )
                return catalog

            logger.error("[DATAVERSE-PREPUBLISH] No catalogs available in database")
            raise ProblemDetailException(
                _("No catalog available"),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        catalog = resolve_catalog()

        # Get system user (superuser)
        logger.info("[DATAVERSE-PREPUBLISH] Looking for system user (superuser)")
        system_user = User.objects.filter(is_superuser=True).first()
        if not system_user:
            logger.error("[DATAVERSE-PREPUBLISH] No superuser found in database")
            raise ProblemDetailException(
                _("No system user available"),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        logger.info(f"[DATAVERSE-PREPUBLISH] Using system user: {system_user.username} (id: {system_user.pk})")

        # Map content types to Acquisition MIME types
        def map_content_type_to_mime(content_type: str) -> str:
            """Map Dataverse content type to Acquisition MIME type"""
            content_type_lower = (content_type or "").lower()
            if "pdf" in content_type_lower or content_type_lower == "application/pdf":
                return Acquisition.AcquisitionMIME.PDF
            elif "epub" in content_type_lower or content_type_lower == "application/epub+zip":
                return Acquisition.AcquisitionMIME.EPUB
            elif "mobi" in content_type_lower or content_type_lower == "application/x-mobipocket-ebook":
                return Acquisition.AcquisitionMIME.MOBI
            elif "webpub" in content_type_lower or content_type_lower == "application/webpub+zip":
                return Acquisition.AcquisitionMIME.READIUM_PACKAGE
            # Default to PDF if unknown
            return Acquisition.AcquisitionMIME.PDF

        # Extract metadata from dataset
        def extract_metadata(dataset_data):
            """Extract metadata from Dataverse dataset response"""
            metadata_blocks = dataset_data.get("metadataBlocks", {})
            citation_block = metadata_blocks.get("citation", {})
            citation_fields = citation_block.get("fields", []) if citation_block else []

            def citation_field_value(type_name):
                for field in citation_fields:
                    if field.get("typeName") == type_name:
                        return field.get("value")
                return None

            def flatten_text(value):
                if value is None:
                    return []
                if isinstance(value, str):
                    cleaned = value.strip()
                    return [cleaned] if cleaned else []
                if isinstance(value, (int, float)):
                    return [str(value)]
                if isinstance(value, list):
                    flattened = []
                    for item in value:
                        flattened.extend(flatten_text(item))
                    return flattened
                if isinstance(value, dict):
                    first_name = value.get("firstName") or value.get("givenName") or ""
                    last_name = value.get("lastName") or value.get("familyName") or ""
                    if first_name or last_name:
                        return [" ".join(part for part in [first_name, last_name] if part).strip()]
                    for key in (
                        "value",
                        "displayValue",
                        "authorName",
                        "name",
                        "keywordValue",
                        "subject",
                        "term",
                        "title",
                    ):
                        if key in value:
                            return flatten_text(value.get(key))
                return []

            def first_text(*values):
                for value in values:
                    flattened = flatten_text(value)
                    if flattened:
                        return flattened[0]
                return None

            def parse_partial_date(value):
                if not isinstance(value, str):
                    return None
                cleaned = value.strip()
                if not cleaned:
                    return None
                from datetime import datetime

                for fmt, include_month, include_day in (
                    ("%Y-%m-%d", True, True),
                    ("%Y-%m", True, False),
                    ("%Y", False, False),
                ):
                    try:
                        dt = datetime.strptime(cleaned, fmt)
                        return PartialDate(
                            year=dt.year,
                            month=dt.month if include_month else None,
                            day=dt.day if include_day else None,
                        )
                    except ValueError:
                        continue
                return None

            def resolve_language(value):
                language_values = flatten_text(value)
                for language_value in language_values:
                    normalized = language_value.strip()
                    if not normalized:
                        continue
                    lowered = normalized.lower()
                    language = None
                    if len(lowered) == 2:
                        language = Language.objects.filter(alpha2=lowered).first()
                    elif len(lowered) == 3:
                        language = Language.objects.filter(alpha3=lowered).first()
                    if language is None:
                        language = Language.objects.filter(name__iexact=normalized).first()
                    if language:
                        return language
                return None

            def build_content(metadata):
                lines = []
                if metadata["summary"]:
                    lines.append(metadata["summary"])
                if metadata["authors"]:
                    lines.append(
                        "Authors: "
                        + ", ".join(
                            " ".join(part for part in [author["name"], author["surname"]] if part).strip()
                            for author in metadata["authors"]
                            if author.get("name") or author.get("surname")
                        )
                    )
                if metadata["publisher"]:
                    lines.append(f"Publisher: {metadata['publisher']}")
                if metadata["language"]:
                    lines.append(f"Language: {metadata['language'].name}")
                if metadata["doi"]:
                    lines.append(f"Persistent ID: {metadata['doi']}")
                file_names = [
                    (item.get("dataFile") or {}).get("filename")
                    for item in items
                    if (item.get("dataFile") or {}).get("filename")
                ]
                if file_names:
                    lines.append("Files: " + ", ".join(file_names))
                return "\n\n".join(line for line in lines if line) or None

            def build_citation(metadata):
                author_names = [
                    " ".join(part for part in [author["surname"], author["name"]] if part).strip(", ")
                    for author in metadata["authors"]
                    if author.get("name") or author.get("surname")
                ]
                citation_parts = []
                if author_names:
                    citation_parts.append("; ".join(author_names))
                if metadata["title"]:
                    citation_parts.append(metadata["title"])
                if metadata["publisher"]:
                    citation_parts.append(metadata["publisher"])
                if metadata["published_at"]:
                    citation_parts.append(str(metadata["published_at"]))
                if metadata["doi"]:
                    citation_parts.append(metadata["doi"])
                return ". ".join(part for part in citation_parts if part) or None

            metadata = {
                "title": title or dataset_data.get("title", ""),
                "summary": None,
                "content": None,
                "authors": [],
                "language": None,
                "publisher": None,
                "published_at": None,
                "doi": None,
                "citation": None,
            }

            # Get title from dataset if not provided
            if not metadata["title"]:
                metadata["title"] = (
                    dataset_data.get("title") or dataset_data.get("displayName") or f"Dataverse Dataset {global_id}"
                )

            # Get description/summary
            metadata["summary"] = first_text(
                dataset_data.get("description"),
                dataset_data.get("descriptionText"),
                citation_field_value("dsDescription"),
            )

            # Get authors from dataset metadataBlocks
            authors_list = []
            if citation_block:
                for field in citation_fields:
                    if field.get("typeName") == "author":
                        authors_list = field.get("value", [])
                        break

            # If no authors found in metadataBlocks, try direct authors field
            if not authors_list:
                authors_list = dataset_data.get("authors", [])

            for author_data in authors_list:
                if isinstance(author_data, dict):
                    # Dataverse author format can be: {"authorName": "John Doe"} or {"firstName": "John", "lastName": "Doe"}
                    author_name = author_data.get("authorName") or author_data.get("name")
                    first_name = author_data.get("firstName") or author_data.get("givenName")
                    last_name = author_data.get("lastName") or author_data.get("familyName")

                    if first_name or last_name:
                        # Use separate first/last name fields
                        metadata["authors"].append({"name": first_name or "", "surname": last_name or ""})
                    elif author_name:
                        # Handle authorName - could be string or dict
                        if isinstance(author_name, str):
                            # Try to split full name into first/last
                            name_parts = author_name.strip().split(None, 1)
                            if len(name_parts) >= 2:
                                metadata["authors"].append(
                                    {"name": name_parts[0], "surname": " ".join(name_parts[1:])}
                                )
                            else:
                                metadata["authors"].append(
                                    {"name": name_parts[0] if name_parts else "", "surname": ""}
                                )
                        elif isinstance(author_name, dict):
                            # If authorName is a dict, extract from it
                            first = author_name.get("firstName") or author_name.get("givenName") or ""
                            last = author_name.get("lastName") or author_name.get("familyName") or ""
                            if first or last:
                                metadata["authors"].append({"name": first, "surname": last})

            # Get publisher
            metadata["publisher"] = first_text(
                dataset_data.get("publisher"),
                dataset_data.get("producer"),
                citation_field_value("publisher"),
                citation_field_value("producerName"),
            )

            # Get publication date
            pub_date = (
                dataset_data.get("publicationDate")
                or dataset_data.get("datePublished")
                or first_text(citation_field_value("productionDate"))
                or first_text(citation_field_value("distributionDate"))
            )
            metadata["published_at"] = parse_partial_date(pub_date)

            # Get DOI
            metadata["doi"] = first_text(
                dataset_data.get("persistentId"),
                dataset_data.get("doi"),
            )
            if metadata["doi"] and not metadata["doi"].startswith("doi:"):
                # Ensure DOI format
                if metadata["doi"].startswith("10."):
                    metadata["doi"] = f"doi:{metadata['doi']}"

            metadata["language"] = resolve_language(citation_field_value("language") or dataset_data.get("language"))
            metadata["content"] = build_content(metadata)
            metadata["citation"] = build_citation(metadata)

            logger.info(
                "[DATAVERSE-PREPUBLISH] Extracted metadata - "
                f"title: {metadata['title']}, authors: {len(metadata['authors'])}, "
                f"summary: {bool(metadata['summary'])}, language: {getattr(metadata['language'], 'alpha2', None)}, "
                f"publisher: {metadata['publisher']}, citation: {bool(metadata['citation'])}"
            )
            return metadata

        extracted_metadata = extract_metadata(dataset_metadata)

        logger.info("[DATAVERSE-PREPUBLISH] Starting database transaction")
        with transaction.atomic():
            # Find or create entry by Dataverse global_id
            logger.info(
                f"[DATAVERSE-PREPUBLISH] Looking for existing entry with dataverse_pid={global_id} in catalog={catalog.url_name}"
            )
            entry = Entry.objects.filter(
                catalog=catalog,
                identifiers__dataverse_pid=global_id,
            ).first()

            if entry:
                logger.info(f"[DATAVERSE-PREPUBLISH] Found existing entry: {entry.pk} - {entry.title}")
                entry_identifiers = dict(entry.identifiers or {})
                entry_identifiers["dataverse_pid"] = global_id
                entry_identifiers["dataverse_dataset_id"] = str(dataset_id)
                if extracted_metadata["doi"]:
                    entry_identifiers["doi"] = extracted_metadata["doi"]

                updated = False
                if extracted_metadata["title"] != entry.title:
                    logger.info(
                        f"[DATAVERSE-PREPUBLISH] Updating entry title from '{entry.title}' to '{extracted_metadata['title']}'"
                    )
                    entry.title = extracted_metadata["title"]
                    updated = True
                if extracted_metadata["summary"] != entry.summary:
                    entry.summary = extracted_metadata["summary"]
                    updated = True
                if extracted_metadata["content"] != entry.content:
                    entry.content = extracted_metadata["content"]
                    updated = True
                if extracted_metadata["publisher"] != entry.publisher:
                    entry.publisher = extracted_metadata["publisher"]
                    updated = True
                if extracted_metadata["published_at"] != entry.published_at:
                    entry.published_at = extracted_metadata["published_at"]
                    updated = True
                if extracted_metadata["citation"] != entry.citation:
                    entry.citation = extracted_metadata["citation"]
                    updated = True
                if extracted_metadata["language"] != entry.language:
                    entry.language = extracted_metadata["language"]
                    updated = True
                if entry_identifiers != (entry.identifiers or {}):
                    entry.identifiers = entry_identifiers
                    updated = True
                if updated:
                    entry.save()
                    logger.info(f"[DATAVERSE-PREPUBLISH] Entry updated successfully")
            else:
                logger.info("[DATAVERSE-PREPUBLISH] No existing entry found, creating new entry")
                entry_identifiers = {
                    "dataverse_pid": global_id,
                    "dataverse_dataset_id": str(dataset_id),
                }

                if extracted_metadata["doi"]:
                    entry_identifiers["doi"] = extracted_metadata["doi"]

                logger.info(
                    f"[DATAVERSE-PREPUBLISH] Creating entry - title: {extracted_metadata['title']}, identifiers: {entry_identifiers}"
                )
                entry = Entry(
                    creator=system_user,
                    catalog=catalog,
                    title=extracted_metadata["title"],
                    summary=extracted_metadata["summary"],
                    content=extracted_metadata["content"],
                    language=extracted_metadata["language"],
                    publisher=extracted_metadata["publisher"],
                    published_at=extracted_metadata["published_at"],
                    citation=extracted_metadata["citation"],
                    identifiers=entry_identifiers,
                )
                entry.save()
                logger.info(f"[DATAVERSE-PREPUBLISH] Created new entry: {entry.pk} - {entry.title}")

            # Add authors
            if extracted_metadata["authors"]:
                logger.info(f"[DATAVERSE-PREPUBLISH] Adding {len(extracted_metadata['authors'])} authors")
                entry.authors.clear()
                for idx, author_data in enumerate(extracted_metadata["authors"]):
                    author, created = Author.objects.get_or_create(
                        catalog=catalog,
                        name=author_data.get("name", ""),
                        surname=author_data.get("surname", ""),
                    )
                    EntryAuthor.objects.get_or_create(entry=entry, author=author, defaults={"position": idx})
                logger.info(f"[DATAVERSE-PREPUBLISH] Added authors to entry")

            # Process each file and create/update acquisitions
            logger.info(f"[DATAVERSE-PREPUBLISH] Processing {len(items)} files for entry {entry.pk}")
            for idx, it in enumerate(items, 1):
                logger.info(f"[DATAVERSE-PREPUBLISH] Processing file {idx}/{len(items)}: {json.dumps(it, indent=2)}")
                df = it.get("dataFile") or {}
                datafile_id = df.get("id")
                filename = df.get("filename")
                content_type = df.get("contentType")

                logger.info(
                    f"[DATAVERSE-PREPUBLISH] File data - datafile_id: {datafile_id}, filename: {filename}, content_type: {content_type}"
                )

                if not datafile_id:
                    logger.warning(f"[DATAVERSE-PREPUBLISH] Skipping file {idx} - no datafile_id found")
                    continue

                browser_url = f"{dv_public_base}/api/access/datafile/{datafile_id}"
                logger.info(f"[DATAVERSE-PREPUBLISH] Generated browser URL: {browser_url}")

                # Map content type to acquisition MIME type
                mime_type = map_content_type_to_mime(content_type)
                logger.info(f"[DATAVERSE-PREPUBLISH] Mapped content_type '{content_type}' to MIME type: {mime_type}")

                # Check if acquisition already exists for this datafile
                logger.info(f"[DATAVERSE-PREPUBLISH] Checking for existing acquisition with file_url={browser_url}")
                existing_acquisition = Acquisition.objects.filter(
                    entry=entry,
                    file_url=browser_url,
                ).first()

                if existing_acquisition:
                    logger.info(f"[DATAVERSE-PREPUBLISH] Found existing acquisition: {existing_acquisition.pk}")
                    # Update if needed
                    if existing_acquisition.mime != mime_type:
                        logger.info(
                            f"[DATAVERSE-PREPUBLISH] Updating acquisition MIME type from {existing_acquisition.mime} to {mime_type}"
                        )
                        existing_acquisition.mime = mime_type
                        existing_acquisition.save()
                        logger.info(f"[DATAVERSE-PREPUBLISH] Acquisition updated successfully")
                    else:
                        logger.info(f"[DATAVERSE-PREPUBLISH] Acquisition already up to date")
                else:
                    logger.info("[DATAVERSE-PREPUBLISH] No existing acquisition found, creating new one")
                    # Create new acquisition with Dataverse URL
                    acquisition = Acquisition(
                        entry=entry,
                        mime=mime_type,
                        file_url=browser_url,
                        relation=Acquisition.AcquisitionType.OPEN_ACCESS,
                        content=None,  # No file stored, only URL
                    )
                    logger.info(
                        f"[DATAVERSE-PREPUBLISH] Saving acquisition - entry: {entry.pk}, mime: {mime_type}, file_url: {browser_url}"
                    )
                    acquisition.save()
                    logger.info(
                        f"[DATAVERSE-PREPUBLISH] Created acquisition: {acquisition.pk} for datafile {datafile_id} filename={filename} contentType={content_type} url={browser_url}"
                    )

        _schedule_dataverse_workflow_resume(dv_base_internal, dv_token, invocation_id)

        logger.info("[DATAVERSE-PREPUBLISH] ===== Request processing completed successfully =====")
        return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")
