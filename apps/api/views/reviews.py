"""Ratings and reviews of entries (IP-023)."""

from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.utils.translation import gettext as _

from apps import openapi
from apps.api.filters.reviews import EntryReviewFilter
from apps.api.forms.reviews import EntryReviewForm, EntryReviewModerationForm
from apps.api.response import Ordering, PaginationResponse, SingleResponse
from apps.api.serializers.reviews import EntryReviewSerializer
from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException, ValidationException
from apps.core.models import Entry, EntryReview, UserCatalog
from apps.core.services.reviews import ReviewService, can_moderate, can_read_entry
from apps.core.views import SecuredView

ORDERINGS = {
    "created_at": ["created_at", "pk"],
    "-created_at": ["-created_at", "-pk"],
    "rating": ["rating", "-created_at", "pk"],
    "-rating": ["-rating", "-created_at", "pk"],
}


def _not_found(title=None):
    return ProblemDetailException(
        title or _("Entry not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
    )


def _require_enabled():
    if not settings.EVILFLOWERS_REVIEWS_ENABLED:
        raise _not_found(_("Reviews are not enabled"))


def _readable_entry(request, entry_id: UUID) -> Entry:
    """The entry, or 404 when it does not exist or the caller may not read it (no existence oracle)."""
    _require_enabled()
    try:
        entry = Entry.objects.select_related("catalog").get(pk=entry_id)
    except Entry.DoesNotExist:
        raise _not_found()
    if not can_read_entry(request.user, entry):
        raise _not_found()
    return entry


def _require_login(request):
    if not request.user.is_authenticated:
        raise UnauthorizedException(detail=_("You need to be logged in to review a publication"))


class EntryReviewList(SecuredView):
    @openapi.metadata(
        description=(
            "Published reviews of an entry, newest first. `order_by` accepts `created_at`, `-created_at`, "
            "`rating` and `-rating`. Catalog managers may add `state=hidden` (or `state=all`) to see hidden "
            "reviews. Anyone who can read the entry may list its reviews."
        ),
        tags=["Reviews"],
        summary="List reviews of an entry",
    )
    def get(self, request, entry_id: UUID):
        entry = _readable_entry(request, entry_id)

        reviews = EntryReview.objects.filter(entry=entry).select_related("user", "language")
        state = request.GET.get("state", EntryReview.State.PUBLISHED)
        if state != EntryReview.State.PUBLISHED and can_moderate(request.user, entry):
            if state in EntryReview.State.values:
                reviews = reviews.filter(state=state)
        else:
            reviews = reviews.filter(state=EntryReview.State.PUBLISHED)

        order_by = request.GET.get("order_by", "-created_at")
        if order_by not in ORDERINGS:
            raise ProblemDetailException(
                _("Invalid order_by"),
                status=HTTPStatus.BAD_REQUEST,
                detail=_("Reviews can be sorted by %(columns)s.") % {"columns": ", ".join(ORDERINGS)},
            )

        serializer = (
            EntryReviewSerializer.Moderated if can_moderate(request.user, entry) else EntryReviewSerializer.Base
        )
        return PaginationResponse(request, reviews, serializer=serializer, ordering=Ordering(ORDERINGS[order_by]))


class ReviewList(SecuredView):
    @openapi.metadata(
        description=(
            "Reviews across entries, for moderation: superusers see every review, catalog managers the reviews "
            "of entries in the catalogs they manage (published and hidden). Others get 403. Filters: `state`, "
            "`catalog_id`, `entry_id`, `user_id`, `rating`, `rating__lte`, `query`. `order_by` accepts "
            "`created_at`, `-created_at` (default), `rating` and `-rating`."
        ),
        tags=["Reviews"],
        summary="List reviews for moderation",
    )
    def get(self, request):
        _require_enabled()
        if not request.user.is_authenticated:
            raise UnauthorizedException(detail=_("You need to be logged in"))
        if (
            not request.user.is_superuser
            and not UserCatalog.objects.filter(user=request.user, mode=UserCatalog.Mode.MANAGE).exists()
        ):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        order_by = request.GET.get("order_by", "-created_at")
        if order_by not in ORDERINGS:
            raise ProblemDetailException(
                _("Invalid order_by"),
                status=HTTPStatus.BAD_REQUEST,
                detail=_("Reviews can be sorted by %(columns)s.") % {"columns": ", ".join(ORDERINGS)},
            )

        reviews = EntryReviewFilter(
            request.GET,
            queryset=EntryReview.objects.select_related("user", "language", "entry"),
            request=request,
        ).qs
        return PaginationResponse(
            request, reviews, serializer=EntryReviewSerializer.Admin, ordering=Ordering(ORDERINGS[order_by])
        )


class OwnEntryReview(SecuredView):
    @openapi.metadata(
        description="The caller's own review of an entry, including whether a moderator hid it. 404 without one.",
        tags=["Reviews"],
        summary="Get my review of an entry",
    )
    def get(self, request, entry_id: UUID):
        entry = _readable_entry(request, entry_id)
        _require_login(request)
        review = EntryReview.objects.filter(entry=entry, user=request.user).select_related("user", "language").first()
        if review is None:
            raise _not_found(_("Review not found"))
        return SingleResponse(request, data=EntryReviewSerializer.Moderated.model_validate(review))

    @openapi.metadata(
        description=(
            "Create or replace the caller's review of an entry: `rating` (1–5, required), optional `title`, "
            "`body` and `language_code`. Idempotent; answers 201 when the review is new, 200 when it replaced "
            "one. A review a moderator hid stays hidden after an edit."
        ),
        tags=["Reviews"],
        summary="Rate or review an entry",
    )
    def put(self, request, entry_id: UUID):
        entry = _readable_entry(request, entry_id)
        _require_login(request)

        form = EntryReviewForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        review, created = ReviewService.upsert(
            request.user,
            entry,
            rating=form.cleaned_data["rating"],
            title=form.cleaned_data.get("title") or "",
            body=form.cleaned_data.get("body") or "",
            language=form.cleaned_data.get("language_code"),
        )
        review = EntryReview.objects.select_related("user", "language").get(pk=review.pk)
        if created:
            return SingleResponse(
                request, data=EntryReviewSerializer.Moderated.model_validate(review), status=HTTPStatus.CREATED
            )
        return SingleResponse(
            request, data=EntryReviewSerializer.Moderated.model_validate(review), status=HTTPStatus.OK
        )

    @openapi.metadata(
        description="Delete the caller's review of an entry. 404 without one.",
        tags=["Reviews"],
        summary="Delete my review of an entry",
    )
    def delete(self, request, entry_id: UUID):
        entry = _readable_entry(request, entry_id)
        _require_login(request)
        review = EntryReview.objects.filter(entry=entry, user=request.user).first()
        if review is None:
            raise _not_found(_("Review not found"))
        ReviewService.delete(review)
        return SingleResponse(request)


class EntryReviewDetail(SecuredView):
    @openapi.metadata(
        description=(
            "Moderate a review: `state` `hidden` or `published`, optional `moderation_note`. Only managers of the "
            "entry's catalog and superusers. A hidden review is shown only to its author and to moderators and "
            "does not count in the entry's rating."
        ),
        tags=["Reviews"],
        summary="Hide or publish a review",
    )
    def patch(self, request, review_id: UUID):
        _require_enabled()
        try:
            review = EntryReview.objects.select_related("entry__catalog").get(pk=review_id)
        except EntryReview.DoesNotExist:
            raise _not_found(_("Review not found"))

        if not can_moderate(request.user, review.entry):
            # Readers of the entry learn that they may not moderate; others learn nothing.
            if can_read_entry(request.user, review.entry) and review.state == EntryReview.State.PUBLISHED:
                raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)
            raise _not_found(_("Review not found"))

        form = EntryReviewModerationForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        note = form.cleaned_data.get("moderation_note")
        ReviewService.moderate(
            review,
            request.user,
            state=form.cleaned_data["state"],
            note=note if "moderation_note" in form.dirty else None,
        )
        review = EntryReview.objects.select_related("user", "language").get(pk=review.pk)
        return SingleResponse(request, data=EntryReviewSerializer.Moderated.model_validate(review))
