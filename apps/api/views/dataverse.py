import json
import logging
import os
from http import HTTPStatus
from django.db import transaction

import requests
from django.http import HttpResponse
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from apps import openapi
from apps.core.errors import ProblemDetailException
from apps.core.models import Entry, Acquisition, Catalog, User, Author, EntryAuthor
from partial_date import PartialDate

logger = logging.getLogger(__name__)


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

        logger.info(f"[DATAVERSE-SYNC] received publish event: dataset_id={dataset_id} global_id={global_id} title={title}")

        return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")


@method_decorator(csrf_exempt, name="dispatch")
class DataversePrepublishIngest(View):
    @openapi.metadata(description="Dataverse workflow pre-publish log file URLs", tags=["Dataverse"])
    def post(self, request):
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
        
        logger.info(f"[DATAVERSE-PREPUBLISH] Extracted values - dataset_id: {dataset_id}, global_id: {global_id}, title: {title}")

        expected_secret = os.getenv("DATAVERSE_WORKFLOW_SECRET", "")
        logger.info(f"[DATAVERSE-PREPUBLISH] Checking secret - provided: {'***' if secret else 'None'}, expected: {'***' if expected_secret else 'None'}")
        if not expected_secret or secret != expected_secret:
            logger.warning(f"[DATAVERSE-PREPUBLISH] Secret validation failed")
            raise ProblemDetailException(_("Forbidden"), status=HTTPStatus.FORBIDDEN)

        if dataset_id is None or global_id is None:
            logger.error(f"[DATAVERSE-PREPUBLISH] Missing required fields - dataset_id: {dataset_id}, global_id: {global_id}")
            raise ProblemDetailException(
                _("Missing required fields: dataset_id and global_id"),
                status=HTTPStatus.BAD_REQUEST,
            )
        
        logger.info(f"[DATAVERSE-PREPUBLISH] Validation passed, proceeding with processing")

        dv_base_internal = os.getenv("DV_BASE_INTERNAL", "http://dataverse:8080").rstrip("/")
        dv_public_base = os.getenv("DV_PUBLIC_BASE", dv_base_internal).rstrip("/")
        dv_token = os.getenv("DV_API_TOKEN", "").strip()

        logger.info(f"[DATAVERSE-PREPUBLISH] Dataverse config - base_internal: {dv_base_internal}, public_base: {dv_public_base}, token: {'***' if dv_token else 'MISSING'}")

        if not dv_token:
            logger.error("[DATAVERSE-PREPUBLISH] DV_API_TOKEN environment variable is missing")
            raise ProblemDetailException(_("Missing DV_API_TOKEN"), status=HTTPStatus.INTERNAL_SERVER_ERROR)

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
                logger.warning(f"[DATAVERSE-PREPUBLISH] Failed to fetch dataset metadata - status: {dataset_resp.status_code}")
        except Exception as e:
            logger.warning(f"[DATAVERSE-PREPUBLISH] Error fetching dataset metadata: {e}", exc_info=True)

        # Fetch files
        files_url = f"{dv_base_internal}/api/datasets/{dataset_id}/versions/:draft/files"
        logger.info(f"[DATAVERSE-PREPUBLISH] Fetching files from Dataverse: {files_url}")
        
        try:
            resp = requests.get(files_url, headers={"X-Dataverse-key": dv_token}, timeout=60)
            logger.info(f"[DATAVERSE-PREPUBLISH] Dataverse API response - status: {resp.status_code}, headers: {dict(resp.headers)}")
        except Exception as e:
            logger.error(f"[DATAVERSE-PREPUBLISH] Failed to connect to Dataverse: {e}", exc_info=True)
            raise ProblemDetailException(
                _("Dataverse connection failed"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=str(e),
            )

        if resp.status_code != 200:
            logger.error(f"[DATAVERSE-PREPUBLISH] Dataverse API error - status: {resp.status_code}, body: {resp.text[:500]}")
            raise ProblemDetailException(
                _("Dataverse file listing failed"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=f"status={resp.status_code} body={resp.text[:2000]}",
            )

        try:
            response_json = resp.json()
            logger.info(f"[DATAVERSE-PREPUBLISH] Dataverse response JSON keys: {list(response_json.keys()) if isinstance(response_json, dict) else 'Not a dict'}")
            items = (response_json or {}).get("data") or []
        except Exception as e:
            logger.error(f"[DATAVERSE-PREPUBLISH] Failed to parse Dataverse response JSON: {e}", exc_info=True)
            logger.error(f"[DATAVERSE-PREPUBLISH] Response text: {resp.text[:500]}")
            raise ProblemDetailException(
                _("Invalid Dataverse response format"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=str(e),
            )

        logger.info(f"[DATAVERSE-PREPUBLISH] Found {len(items)} files in dataset - dataset_id: {dataset_id}, global_id: {global_id}, title: {title}")

        def resolve_catalog(dataset_id_value, global_id_value) -> Catalog:
            """
            Resolve target catalog using a JSON map (Dataverse → EvilFlowers) with fallbacks.
            Priority:
            1) dataset_id map
            2) global_id prefix map (longest prefix wins)
            3) DATAVERSE_CATALOG_URL_NAME env
            4) map default
            5) first catalog
            """
            logger.info(f"[DATAVERSE-PREPUBLISH] Resolving catalog for dataset_id={dataset_id_value}, global_id={global_id_value}")

            map_path = os.getenv("DATAVERSE_CATALOG_MAP_FILE", "conf/dataverse_catalog_map.json")
            logger.info(f"[DATAVERSE-PREPUBLISH] Using catalog map file: {map_path}")
            mapping = {}
            if os.path.exists(map_path):
                try:
                    with open(map_path, "r", encoding="utf-8") as fh:
                        mapping = json.load(fh) or {}
                    logger.info(f"[DATAVERSE-PREPUBLISH] Loaded catalog map: {json.dumps(mapping, indent=2)}")
                except Exception as exc:
                    logger.warning(f"[DATAVERSE-PREPUBLISH] Failed to load map file {map_path}: {exc}", exc_info=True)
            else:
                logger.info(f"[DATAVERSE-PREPUBLISH] Catalog map file not found at {map_path}, using fallbacks")

            dataset_map = mapping.get("dataset_id", {}) or {}
            prefix_map = mapping.get("global_id_prefix", {}) or {}
            default_url_name = mapping.get("default")
            
            logger.info(f"[DATAVERSE-PREPUBLISH] Map contents - dataset_map keys: {list(dataset_map.keys())}, prefix_map keys: {list(prefix_map.keys())}, default: {default_url_name}")

            catalog_url_name = None

            # 1) dataset_id exact match
            if dataset_id_value is not None:
                catalog_url_name = dataset_map.get(str(dataset_id_value))
                if catalog_url_name:
                    logger.info(f"[DATAVERSE-PREPUBLISH] Found catalog via dataset_id map: {catalog_url_name}")

            # 2) global_id prefix (longest match)
            if catalog_url_name is None and global_id_value and prefix_map:
                for prefix in sorted(prefix_map.keys(), key=len, reverse=True):
                    if str(global_id_value).startswith(prefix):
                        catalog_url_name = prefix_map[prefix]
                        logger.info(f"[DATAVERSE-PREPUBLISH] Found catalog via global_id prefix '{prefix}': {catalog_url_name}")
                        break

            # 3) explicit env override
            if catalog_url_name is None:
                catalog_url_name = os.getenv("DATAVERSE_CATALOG_URL_NAME")
                if catalog_url_name:
                    logger.info(f"[DATAVERSE-PREPUBLISH] Using catalog from DATAVERSE_CATALOG_URL_NAME env: {catalog_url_name}")

            # 4) map default
            if catalog_url_name is None and default_url_name:
                catalog_url_name = default_url_name
                logger.info(f"[DATAVERSE-PREPUBLISH] Using catalog from map default: {catalog_url_name}")

            # 5) fallback to first catalog
            if catalog_url_name:
                try:
                    catalog = Catalog.objects.get(url_name=catalog_url_name)
                    logger.info(f"[DATAVERSE-PREPUBLISH] Resolved catalog: {catalog.url_name} (id: {catalog.pk}, title: {catalog.title})")
                    return catalog
                except Catalog.DoesNotExist:
                    logger.error(f"[DATAVERSE-PREPUBLISH] Catalog not found: {catalog_url_name}")
                    raise ProblemDetailException(
                        _("Catalog not found: %s") % catalog_url_name,
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )

            logger.info("[DATAVERSE-PREPUBLISH] No specific catalog found, using first available catalog")
            catalog_fallback = Catalog.objects.first()
            if not catalog_fallback:
                logger.error("[DATAVERSE-PREPUBLISH] No catalogs available in database")
                raise ProblemDetailException(
                    _("No catalog available"),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            logger.info(f"[DATAVERSE-PREPUBLISH] Using fallback catalog: {catalog_fallback.url_name} (id: {catalog_fallback.pk})")
            return catalog_fallback

        catalog = resolve_catalog(dataset_id, global_id)

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
            metadata = {
                "title": title or dataset_data.get("title", ""),
                "summary": None,
                "authors": [],
                "publisher": None,
                "published_at": None,
                "doi": None,
            }
            
            # Get title from dataset if not provided
            if not metadata["title"]:
                metadata["title"] = dataset_data.get("title") or dataset_data.get("displayName") or f"Dataverse Dataset {global_id}"
            
            # Get description/summary
            metadata["summary"] = dataset_data.get("description") or dataset_data.get("descriptionText")
            
            # Get authors from dataset metadataBlocks
            authors_list = []
            metadata_blocks = dataset_data.get("metadataBlocks", {})
            citation_block = metadata_blocks.get("citation", {})
            if citation_block:
                fields = citation_block.get("fields", [])
                for field in fields:
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
                        metadata["authors"].append({
                            "name": first_name or "",
                            "surname": last_name or ""
                        })
                    elif author_name:
                        # Handle authorName - could be string or dict
                        if isinstance(author_name, str):
                            # Try to split full name into first/last
                            name_parts = author_name.strip().split(None, 1)
                            if len(name_parts) >= 2:
                                metadata["authors"].append({"name": name_parts[0], "surname": " ".join(name_parts[1:])})
                            else:
                                metadata["authors"].append({"name": name_parts[0] if name_parts else "", "surname": ""})
                        elif isinstance(author_name, dict):
                            # If authorName is a dict, extract from it
                            first = author_name.get("firstName") or author_name.get("givenName") or ""
                            last = author_name.get("lastName") or author_name.get("familyName") or ""
                            if first or last:
                                metadata["authors"].append({"name": first, "surname": last})
            
            # Get publisher
            metadata["publisher"] = dataset_data.get("publisher") or dataset_data.get("producer")
            
            # Get publication date
            pub_date = dataset_data.get("publicationDate") or dataset_data.get("datePublished")
            if pub_date:
                try:
                    # Try to parse date
                    from datetime import datetime
                    if isinstance(pub_date, str):
                        # Try common formats
                        for fmt in ["%Y-%m-%d", "%Y", "%Y-%m"]:
                            try:
                                dt = datetime.strptime(pub_date[:len(fmt)], fmt)
                                metadata["published_at"] = PartialDate(year=dt.year, month=dt.month if fmt != "%Y" else None, day=dt.day if fmt == "%Y-%m-%d" else None)
                                break
                            except:
                                continue
                except:
                    pass
            
            # Get DOI
            metadata["doi"] = dataset_data.get("persistentId") or dataset_data.get("doi")
            if metadata["doi"] and not metadata["doi"].startswith("doi:"):
                # Ensure DOI format
                if metadata["doi"].startswith("10."):
                    metadata["doi"] = f"doi:{metadata['doi']}"
            
            logger.info(f"[DATAVERSE-PREPUBLISH] Extracted metadata - title: {metadata['title']}, authors: {len(metadata['authors'])}, summary: {bool(metadata['summary'])}, publisher: {metadata['publisher']}")
            return metadata

        extracted_metadata = extract_metadata(dataset_metadata)

        logger.info("[DATAVERSE-PREPUBLISH] Starting database transaction")
        with transaction.atomic():
            # Find or create entry by Dataverse global_id
            logger.info(f"[DATAVERSE-PREPUBLISH] Looking for existing entry with dataverse_pid={global_id} in catalog={catalog.url_name}")
            entry = Entry.objects.filter(
                catalog=catalog,
                identifiers__dataverse_pid=global_id,
            ).first()

            if entry:
                logger.info(f"[DATAVERSE-PREPUBLISH] Found existing entry: {entry.pk} - {entry.title}")
                # Update existing entry with new metadata
                updated = False
                if extracted_metadata["title"] and extracted_metadata["title"] != entry.title:
                    logger.info(f"[DATAVERSE-PREPUBLISH] Updating entry title from '{entry.title}' to '{extracted_metadata['title']}'")
                    entry.title = extracted_metadata["title"]
                    updated = True
                if extracted_metadata["summary"] and extracted_metadata["summary"] != entry.summary:
                    entry.summary = extracted_metadata["summary"]
                    updated = True
                if extracted_metadata["publisher"] and extracted_metadata["publisher"] != entry.publisher:
                    entry.publisher = extracted_metadata["publisher"]
                    updated = True
                if extracted_metadata["published_at"] and extracted_metadata["published_at"] != entry.published_at:
                    entry.published_at = extracted_metadata["published_at"]
                    updated = True
                if updated:
                    entry.save()
                    logger.info(f"[DATAVERSE-PREPUBLISH] Entry updated successfully")
            else:
                logger.info("[DATAVERSE-PREPUBLISH] No existing entry found, creating new entry")
                # Create new entry with full metadata
                entry_identifiers = {"dataverse_pid": global_id, "dataverse_dataset_id": str(dataset_id)}
                if extracted_metadata["doi"]:
                    entry_identifiers["doi"] = extracted_metadata["doi"]
                
                logger.info(f"[DATAVERSE-PREPUBLISH] Creating entry - title: {extracted_metadata['title']}, identifiers: {entry_identifiers}")
                entry = Entry(
                    creator=system_user,
                    catalog=catalog,
                    title=extracted_metadata["title"],
                    summary=extracted_metadata["summary"],
                    publisher=extracted_metadata["publisher"],
                    published_at=extracted_metadata["published_at"],
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
                    EntryAuthor.objects.get_or_create(
                        entry=entry,
                        author=author,
                        defaults={"position": idx}
                    )
                logger.info(f"[DATAVERSE-PREPUBLISH] Added authors to entry")

            # Process each file and create/update acquisitions
            logger.info(f"[DATAVERSE-PREPUBLISH] Processing {len(items)} files for entry {entry.pk}")
            for idx, it in enumerate(items, 1):
                logger.info(f"[DATAVERSE-PREPUBLISH] Processing file {idx}/{len(items)}: {json.dumps(it, indent=2)}")
                df = it.get("dataFile") or {}
                datafile_id = df.get("id")
                filename = df.get("filename")
                content_type = df.get("contentType")
                
                logger.info(f"[DATAVERSE-PREPUBLISH] File data - datafile_id: {datafile_id}, filename: {filename}, content_type: {content_type}")
                
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
                        logger.info(f"[DATAVERSE-PREPUBLISH] Updating acquisition MIME type from {existing_acquisition.mime} to {mime_type}")
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
                    logger.info(f"[DATAVERSE-PREPUBLISH] Saving acquisition - entry: {entry.pk}, mime: {mime_type}, file_url: {browser_url}")
                    acquisition.save()
                    logger.info(f"[DATAVERSE-PREPUBLISH] Created acquisition: {acquisition.pk} for datafile {datafile_id} filename={filename} contentType={content_type} url={browser_url}")

        logger.info("[DATAVERSE-PREPUBLISH] ===== Request processing completed successfully =====")
        return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")