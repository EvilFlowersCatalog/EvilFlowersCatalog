from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.core.errors import DetailType, ValidationException, ProblemDetailException
from apps.api.filters.feeds import FeedFilter
from apps.api.forms.feeds import FeedForm
from apps.api.response import SingleResponse, PaginationResponse, Ordering
from apps.core.fields.localized import localize, localized_sort_key, sort_language
from apps.api.serializers.feeds import FeedSerializer, feed_queryset
from apps.core.models import Feed
from apps.core.views import SecuredView


def assert_url_name_available(catalog, url_name: str, *, exclude_pk=None) -> None:
    """`(catalog, url_name)` is the only uniqueness rule on a feed; titles may repeat.

    The 409 names the feed holding the slug and offers the first free alternative,
    so a client can resolve the clash without another round trip.
    """
    clashes = Feed.objects.filter(catalog=catalog, url_name=url_name)
    if exclude_pk is not None:
        clashes = clashes.exclude(pk=exclude_pk)

    existing = clashes.first()
    if existing is None:
        return

    suggestion = Feed.available_url_name(catalog, url_name, exclude_pk=exclude_pk)
    raise ProblemDetailException(
        _("Feed with same url_name already exists in same catalog"),
        status=HTTPStatus.CONFLICT,
        detail_type=DetailType.FEED_URL_NAME_TAKEN,
        detail=_("The URL name '%(url_name)s' is already used by feed '%(title)s'. Try '%(suggestion)s'.")
        % {"url_name": url_name, "title": localize(existing.title), "suggestion": suggestion},
        additional_data={
            "field": "url_name",
            "url_name": url_name,
            "suggested_url_name": suggestion,
            "conflicting_feed": {
                "id": str(existing.pk),
                "title": existing.title,
                "url_name": existing.url_name,
            },
        },
    )


class FeedManagement(SecuredView):
    @openapi.metadata(
        description="Create a new feed to organize and categorize entries within a catalog. Feeds can be used to group related publications, create reading lists, or organize content by topic, genre, or any custom criteria.",
        tags=["Feeds"],
        summary="Create a new feed",
    )
    def post(self, request):
        form = FeedForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        if not has_object_permission("check_catalog_manage", request.user, form.cleaned_data["catalog_id"]):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        assert_url_name_available(form.cleaned_data["catalog_id"], form.cleaned_data["url_name"])

        feed = Feed(creator=request.user)
        form.populate(feed)
        feed.save()

        if "entries" in form.cleaned_data.keys():
            feed.entries.add(*form.cleaned_data["entries"])

        if "parents" in form.cleaned_data.keys():
            feed.parents.add(*form.cleaned_data["parents"])

        return SingleResponse(
            request,
            data=FeedSerializer.Base.model_validate(feed, context={"request": request}),
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(
        description="Retrieve a paginated list of feeds with filtering options. Supports filtering by creator, catalog, title, kind (navigation/acquisition), and parent feed relationships.",
        tags=["Feeds"],
        summary="List feeds with filtering",
    )
    def get(self, request):
        feeds = FeedFilter(request.GET, queryset=feed_queryset(), request=request).qs
        # `order_by=title` sorts by the translation in `?language=` (falling back like `localize`)
        feeds = feeds.annotate(title_sort=localized_sort_key("title", sort_language(request)))

        return PaginationResponse(
            request,
            feeds,
            serializer=FeedSerializer.Base,
            serializer_context={"request": request},
            ordering=Ordering.create_from_request(request, aliases={"title": "title_sort"}),
        )


class FeedDetail(SecuredView):
    @staticmethod
    def _get_feed(request, feed_id: UUID, checker: str = "check_catalog_manage") -> Feed:
        try:
            feed = Feed.objects.select_related("catalog").get(pk=feed_id)
        except Feed.DoesNotExist as e:
            raise ProblemDetailException(_("Feed not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission(checker, request.user, feed.catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return feed

    @openapi.metadata(
        description="Retrieve detailed information about a specific feed including its metadata, content, associated entries, and hierarchical relationships with parent and child feeds.",
        tags=["Feeds"],
        summary="Get feed details",
    )
    def get(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id, "check_catalog_read")

        return SingleResponse(request, data=FeedSerializer.Base.model_validate(feed, context={"request": request}))

    @openapi.metadata(
        description="Update feed metadata including title, content, associated entries, and feed relationships. Allows modification of feed organization and content curation.",
        tags=["Feeds"],
        summary="Update feed metadata",
    )
    def put(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id)

        form = FeedForm.create_from_request(request)
        form.fields["parents"].queryset = form.fields["parents"].queryset.exclude(pk=feed.pk)

        if not form.is_valid():
            raise ValidationException(form)

        assert_url_name_available(form.cleaned_data["catalog_id"], form.cleaned_data["url_name"], exclude_pk=feed.pk)

        form.populate(feed)
        feed.save()

        if feed.kind == Feed.FeedKind.ACQUISITION and "entries" in form.cleaned_data.keys():
            feed.entries.clear()
            feed.entries.add(*form.cleaned_data["entries"])

        if "parents" in form.cleaned_data.keys():
            feed.parents.clear()
            feed.parents.add(*form.cleaned_data["parents"])

        return SingleResponse(request, data=FeedSerializer.Base.model_validate(feed, context={"request": request}))

    @openapi.metadata(
        description="Permanently delete a feed and remove its organizational structure. This does not delete the entries within the feed, only the feed container itself. This action is irreversible.",
        tags=["Feeds"],
        summary="Delete feed",
    )
    def delete(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id)
        feed.delete()

        return SingleResponse(request)
