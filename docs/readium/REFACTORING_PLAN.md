# Readium LCP Integration Refactoring Plan

## Overview

This document outlines the required fixes for the Readium LCP integration in Evil Flowers Catalog. It is intended to be consumed by a coding agent to implement the changes systematically.

**Design Decisions Made:**
- License model remains separate from UserAcquisition (simpler approach)
- Encryption triggers automatically on Acquisition creation using proper service
- Users without a passphrase set cannot create licenses (fail with clear error)

---

## Task 1: Remove Legacy Encryption Signal

**Priority:** CRITICAL
**Files to modify:** `apps/core/models/acquisition.py`

### Problem

The legacy signal at lines 105-116 conflicts with the new `ContentEncryptionService`:
- Uses `acquisition.pk` as `contentid` instead of proper UUID
- Doesn't create `EncryptedContent` record
- Hardcodes `.pdf` extension regardless of actual file type
- Bypasses the service layer entirely

### Required Changes

1. **Remove the legacy lcpencrypt block** from the `background_tasks` signal handler (lines 105-116)

2. **Replace with proper service call:**

```python
@receiver(post_save, sender=Acquisition)
def background_tasks(sender, instance: Acquisition, created: bool, **kwargs):
    event_broker = get_event_broker()

    # OCR task (keep existing logic)
    if created and instance.entry.language_id:
        event_broker.execute(
            "evilflowers_ocr_worker.ocr",
            {
                "args": [instance.content.name, instance.content.name, instance.entry.language.alpha3],
            },
        )

    # Readium encryption via proper service
    if created and instance.entry.read_config("readium_enabled"):
        from apps.readium.services import ContentEncryptionService
        try:
            ContentEncryptionService.encrypt_acquisition(instance)
        except ValueError as e:
            # Log but don't fail - encryption can be triggered manually later
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to trigger encryption for acquisition {instance.pk}: {e}")
```

3. **Add required import** at top of file:
```python
# No new imports needed - ContentEncryptionService import is done inside function to avoid circular imports
```

### Verification

After this change:
- New Acquisitions for readium-enabled entries should create `EncryptedContent` records
- `EncryptedContent.lcp_content_id` should be a UUID (not acquisition PK)
- `EncryptedContent.status` should progress: PENDING → ENCRYPTING → COMPLETED → REGISTERED

---

## Task 2: Fix Acquisition Filter in LicenseService

**Priority:** CRITICAL
**Files to modify:** `apps/readium/services/license_service.py`

### Problem

Line 238-240 filters by non-existent field `acquisition_type="epub"`. The Acquisition model uses `mime` field with values like `application/epub+zip`.

### Required Changes

1. **Fix the filter** at line 238-242:

**Current (broken):**
```python
acquisition = entry.acquisitions.filter(
    entry=entry, acquisition_type="epub"
).first()
if not acquisition:
    raise ValueError("Entry has no EPUB acquisition")
```

**Replace with:**
```python
from apps.core.models import Acquisition

# Find acquisition suitable for LCP (EPUB or PDF)
acquisition = entry.acquisitions.filter(
    mime__in=[
        Acquisition.AcquisitionMIME.EPUB,
        Acquisition.AcquisitionMIME.PDF,
    ]
).first()
if not acquisition:
    raise ValueError("Entry has no EPUB or PDF acquisition suitable for LCP protection")
```

2. **Add import** at top of file (if not present):
```python
from apps.core.models import Entry, User, Acquisition
```

### Verification

- License creation should find acquisitions with EPUB or PDF mime types
- Error message should be clear when no suitable acquisition exists

---

## Task 3: Fix Hash Case Inconsistency

**Priority:** HIGH
**Files to modify:** `apps/readium/services/license_service.py`

### Problem

Line 230 uses lowercase hex hash, but LCP spec requires uppercase. The `LCPServerClient.hash_passphrase()` correctly returns uppercase.

### Required Changes

1. **Replace line 230:**

**Current:**
```python
passphrase_hash = hashlib.sha256(user_passphrase.encode()).hexdigest()
```

**Replace with:**
```python
passphrase_hash = LCPServerClient.hash_passphrase(user_passphrase)
```

2. **Remove unused import** `hashlib` from the file (line 17) if no longer needed after this change.

3. **Add import** if not present:
```python
from .lcp_server_client import LCPServerClient
```

### Verification

- All passphrase hashes should be uppercase hex strings (64 characters)
- Hashes stored in `License.passphrase_hash` should match format expected by LCP server

---

## Task 4: Add User Passphrase Management Endpoint

**Priority:** HIGH
**Files to modify:**
- `apps/api/views/users.py`
- `apps/api/serializers/users.py`
- `apps/api/urls.py`

### Problem

Users have no way to set their default LCP passphrase via API. The fields exist on the User model but no endpoint exposes them.

### Required Changes

#### 4.1 Add new view class in `apps/api/views/users.py`

Add after the `UserMe` class:

```python
class UserLCPPassphrase(SecuredView):
    """Manage user's default LCP passphrase for Readium content protection."""

    @staticmethod
    def _get_user(request, user_id: UUID) -> User:
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist as e:
            raise ProblemDetailException(_("User not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        # Users can only manage their own passphrase, or admins can manage any
        if not (user.id == request.user.id or request.user.has_perm("core.change_user")):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return user

    @openapi.metadata(
        description="Check if user has an LCP passphrase set. Returns passphrase hint if available.",
        tags=["Users", "Readium"],
        summary="Get LCP passphrase status",
    )
    def get(self, request, user_id: UUID):
        user = self._get_user(request, user_id)

        return SingleResponse(request, data={
            "has_passphrase": bool(user.lcp_passphrase_hash),
            "passphrase_hint": user.lcp_passphrase_hint,
        })

    @openapi.metadata(
        description="""
        Set or update user's default LCP passphrase for Readium content protection.

        The passphrase is hashed (SHA-256) before storage - the plain text passphrase
        is never stored. Users must remember their passphrase to unlock LCP-protected
        content in reading applications.

        The optional hint helps users remember their passphrase.
        """,
        tags=["Users", "Readium"],
        summary="Set LCP passphrase",
    )
    def put(self, request, user_id: UUID):
        user = self._get_user(request, user_id)

        passphrase = request.data.get("passphrase")
        hint = request.data.get("hint")

        if not passphrase:
            raise ProblemDetailException(
                _("Passphrase is required"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        if len(passphrase) < 4:
            raise ProblemDetailException(
                _("Passphrase must be at least 4 characters"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        # Import here to avoid circular dependency
        from apps.readium.services.lcp_server_client import LCPServerClient

        # Hash and store
        user.lcp_passphrase_hash = LCPServerClient.hash_passphrase(passphrase)
        user.lcp_passphrase_hint = hint
        user.save()

        return SingleResponse(request, data={
            "message": "LCP passphrase set successfully",
            "has_passphrase": True,
            "passphrase_hint": user.lcp_passphrase_hint,
        })

    @openapi.metadata(
        description="Remove user's default LCP passphrase. Existing licenses will continue to work with their stored passphrase hashes.",
        tags=["Users", "Readium"],
        summary="Remove LCP passphrase",
    )
    def delete(self, request, user_id: UUID):
        user = self._get_user(request, user_id)

        user.lcp_passphrase_hash = None
        user.lcp_passphrase_hint = None
        user.save()

        return SingleResponse(request, data={
            "message": "LCP passphrase removed",
            "has_passphrase": False,
        })
```

#### 4.2 Add import for DetailType in `apps/api/views/users.py`

Ensure this import exists:
```python
from apps.core.errors import (
    ValidationException,
    ProblemDetailException,
    UnauthorizedException,
    DetailType,  # Add if not present
)
```

#### 4.3 Add URL route in `apps/api/urls.py`

Find the user-related URL patterns and add:

```python
path("users/<uuid:user_id>/lcp-passphrase", UserLCPPassphrase.as_view(), name="user-lcp-passphrase"),
```

Also add the import:
```python
from apps.api.views.users import UserManagement, UserDetail, UserMe, UserLCPPassphrase
```

#### 4.4 Update UserSerializer in `apps/api/serializers/users.py`

Add passphrase status to Detailed serializer:

```python
class Detailed(Base):
    permissions: List[str]
    catalog_permissions: dict[UUID, UserCatalog.Mode] = Field(default=dict)
    has_lcp_passphrase: bool = Field(default=False)
    lcp_passphrase_hint: Optional[str] = None

    @classmethod
    def model_validate(cls, obj, **kwargs):
        # Add computed field for passphrase status
        data = super().model_validate(obj, **kwargs)
        data.has_lcp_passphrase = bool(obj.lcp_passphrase_hash)
        data.lcp_passphrase_hint = obj.lcp_passphrase_hint
        return data
```

Note: The serializer change may need adjustment based on the actual Pydantic serializer pattern used. If `model_validate` override doesn't work, add these as properties to the User model instead.

### Verification

- `GET /api/users/{id}/lcp-passphrase` returns passphrase status
- `PUT /api/users/{id}/lcp-passphrase` with `{"passphrase": "test", "hint": "my hint"}` sets passphrase
- `DELETE /api/users/{id}/lcp-passphrase` removes passphrase
- Users can only manage their own passphrase (unless admin)

---

## Task 5: Improve Error Messages for Missing Passphrase

**Priority:** MEDIUM
**Files to modify:** `apps/readium/services/license_service.py`

### Problem

The error message when user has no passphrase is not user-friendly and doesn't guide users to the solution.

### Required Changes

1. **Improve error message** at lines 220-223:

**Current:**
```python
if user_passphrase is None:
    if not user.lcp_passphrase_hash:
        raise ValueError(
            "User must provide a passphrase or have a default LCP passphrase set in their profile"
        )
```

**Replace with:**
```python
if user_passphrase is None:
    if not user.lcp_passphrase_hash:
        raise ValueError(
            "No LCP passphrase available. Please set your default passphrase via "
            "PUT /api/users/{user_id}/lcp-passphrase or provide 'user_passphrase' in this request."
        )
```

### Verification

- Error message clearly tells user how to fix the issue
- Provides both options (set default or provide in request)

---

## Task 6: Add Encryption Status Check Endpoint

**Priority:** MEDIUM
**Files to modify:**
- `apps/readium/views/availability.py` (or create new view)
- `apps/readium/urls.py`

### Problem

There's no way to check if an entry's content is ready for licensing (encryption completed and registered).

### Required Changes

#### 6.1 Add method to EntryAvailabilityView or create separate view

Option A - Extend existing availability endpoint response:

In `apps/readium/views/availability.py`, modify the response to include encryption status:

```python
class EntryAvailabilityView(SecuredView):
    def get(self, request, entry_id: UUID):
        # ... existing code ...

        # Add encryption status
        encryption_status = None
        if entry.read_config("readium_enabled"):
            # Get the primary acquisition for this entry
            from apps.core.models import Acquisition
            acquisition = entry.acquisitions.filter(
                mime__in=[
                    Acquisition.AcquisitionMIME.EPUB,
                    Acquisition.AcquisitionMIME.PDF,
                ]
            ).first()

            if acquisition:
                if hasattr(acquisition, "encrypted_content"):
                    ec = acquisition.encrypted_content
                    encryption_status = {
                        "status": ec.status,
                        "ready_for_licensing": ec.status == "registered",
                        "encrypted_at": ec.encrypted_at.isoformat() if ec.encrypted_at else None,
                        "error_message": ec.error_message,
                    }
                else:
                    encryption_status = {
                        "status": "not_started",
                        "ready_for_licensing": False,
                        "encrypted_at": None,
                        "error_message": None,
                    }

        # Include in response
        availability_data["encryption"] = encryption_status
```

### Verification

- `GET /api/readium/entries/{id}/availability` includes encryption status
- Status shows: not_started, pending, encrypting, completed, failed, or registered
- `ready_for_licensing` boolean indicates if licenses can be created

---

## Task 7: Add Manual Encryption Trigger Endpoint

**Priority:** LOW
**Files to modify:**
- `apps/readium/views/` (new file or add to existing)
- `apps/readium/urls.py`

### Problem

If automatic encryption fails or needs to be re-triggered, there's no API endpoint to do so.

### Required Changes

#### 7.1 Create new view (or add to existing encryption-related view)

```python
# apps/readium/views/encryption.py (new file)

from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import SingleResponse
from apps.core.errors import ProblemDetailException, DetailType
from apps.core.models import Entry, Acquisition
from apps.core.views import SecuredView
from apps.readium.services import ContentEncryptionService


class EntryEncryptionView(SecuredView):
    """Manage encryption for entry content."""

    @openapi.metadata(
        description="Get encryption status for an entry's acquisition.",
        tags=["Readium", "Encryption"],
        summary="Get encryption status",
    )
    def get(self, request, entry_id: UUID):
        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(
                _("Entry not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        if not entry.read_config("readium_enabled"):
            return SingleResponse(request, data={
                "readium_enabled": False,
                "message": "Readium is not enabled for this entry",
            })

        acquisition = entry.acquisitions.filter(
            mime__in=[
                Acquisition.AcquisitionMIME.EPUB,
                Acquisition.AcquisitionMIME.PDF,
            ]
        ).first()

        if not acquisition:
            return SingleResponse(request, data={
                "readium_enabled": True,
                "has_acquisition": False,
                "message": "No suitable acquisition found",
            })

        if hasattr(acquisition, "encrypted_content"):
            ec = acquisition.encrypted_content
            return SingleResponse(request, data={
                "readium_enabled": True,
                "has_acquisition": True,
                "encryption": {
                    "status": ec.status,
                    "lcp_content_id": ec.lcp_content_id,
                    "ready_for_licensing": ec.status == "registered",
                    "encrypted_at": ec.encrypted_at.isoformat() if ec.encrypted_at else None,
                    "registered_at": ec.registered_at.isoformat() if ec.registered_at else None,
                    "error_message": ec.error_message,
                },
            })

        return SingleResponse(request, data={
            "readium_enabled": True,
            "has_acquisition": True,
            "encryption": None,
            "message": "Encryption not started",
        })

    @openapi.metadata(
        description="""
        Trigger encryption for an entry's acquisition.

        Use this to:
        - Start encryption if it hasn't been triggered
        - Re-trigger encryption after a failure (with force=true)

        Requires admin permissions.
        """,
        tags=["Readium", "Encryption"],
        summary="Trigger encryption",
    )
    def post(self, request, entry_id: UUID):
        # Require admin permission for manual encryption trigger
        if not request.user.has_perm("core.change_entry"):
            raise ProblemDetailException(
                _("Insufficient permissions"),
                status=HTTPStatus.FORBIDDEN,
            )

        try:
            entry = Entry.objects.get(pk=entry_id)
        except Entry.DoesNotExist:
            raise ProblemDetailException(
                _("Entry not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        if not entry.read_config("readium_enabled"):
            raise ProblemDetailException(
                _("Readium is not enabled for this entry"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        acquisition = entry.acquisitions.filter(
            mime__in=[
                Acquisition.AcquisitionMIME.EPUB,
                Acquisition.AcquisitionMIME.PDF,
            ]
        ).first()

        if not acquisition:
            raise ProblemDetailException(
                _("No suitable acquisition found for encryption"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        force = request.data.get("force", False)

        # Check if already encrypted
        if hasattr(acquisition, "encrypted_content") and not force:
            ec = acquisition.encrypted_content
            if ec.status in ["encrypting", "completed", "registered"]:
                raise ProblemDetailException(
                    _("Encryption already in progress or completed. Use force=true to re-trigger."),
                    status=HTTPStatus.CONFLICT,
                    detail_type=DetailType.CONFLICT,
                )
            # If failed, allow re-trigger without force
            if ec.status == "failed":
                ec.delete()

        try:
            encrypted_content = ContentEncryptionService.encrypt_acquisition(acquisition)
            return SingleResponse(request, data={
                "message": "Encryption triggered successfully",
                "lcp_content_id": encrypted_content.lcp_content_id,
                "status": encrypted_content.status,
            }, status=HTTPStatus.ACCEPTED)

        except ValueError as e:
            raise ProblemDetailException(
                str(e),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )
```

#### 7.2 Add URL routes in `apps/readium/urls.py`

```python
from apps.readium.views.encryption import EntryEncryptionView

# Add to urlpatterns:
path("entries/<uuid:entry_id>/encryption", EntryEncryptionView.as_view(), name="entry-encryption"),
```

### Verification

- `GET /api/readium/entries/{id}/encryption` returns encryption status
- `POST /api/readium/entries/{id}/encryption` triggers encryption (admin only)
- `POST` with `force=true` re-triggers even if already encrypted

---

## Implementation Order

Execute tasks in this order to minimize breakage:

1. **Task 3** - Fix hash case (quick fix, no API changes)
2. **Task 2** - Fix acquisition filter (critical for license creation)
3. **Task 1** - Remove legacy signal and use service (depends on Task 2)
4. **Task 4** - Add passphrase endpoint (enables user flow)
5. **Task 5** - Improve error messages (polish)
6. **Task 6** - Add encryption status to availability (useful info)
7. **Task 7** - Add manual encryption trigger (admin tooling)

---

## Testing Checklist

After all tasks are complete, verify:

- [ ] New Acquisition on readium-enabled Entry creates EncryptedContent with UUID lcp_content_id
- [ ] EncryptedContent status progresses correctly via webhook
- [ ] User can set LCP passphrase via API
- [ ] User can check passphrase status via API
- [ ] License creation works with user's default passphrase
- [ ] License creation fails with clear error when no passphrase set
- [ ] License creation works with inline passphrase override
- [ ] Passphrase hashes are uppercase in both License and User models
- [ ] Encryption status visible in availability endpoint
- [ ] Admin can manually trigger encryption
- [ ] License download (.lcpl) works correctly

---

## Files Modified Summary

| File | Tasks |
|------|-------|
| `apps/core/models/acquisition.py` | Task 1 |
| `apps/readium/services/license_service.py` | Tasks 2, 3, 5 |
| `apps/api/views/users.py` | Task 4 |
| `apps/api/serializers/users.py` | Task 4 |
| `apps/api/urls.py` | Task 4 |
| `apps/readium/views/availability.py` | Task 6 |
| `apps/readium/views/encryption.py` (new) | Task 7 |
| `apps/readium/urls.py` | Task 7 |