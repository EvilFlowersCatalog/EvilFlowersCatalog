from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.feeds import FeedFilter
from apps.api.forms.feeds import FeedForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.feeds import FeedSerializer
from apps.core.models import Feed
from apps.core.views import SecuredView


class FeedManagement(SecuredView):
    @openapi.metadata(description="Create Feed", tags=["Feeds"])
    def post(self, request):
        form = FeedForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        if not has_object_permission("check_catalog_read", request.user, form.cleaned_data["catalog_id"]):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        # FIXME: Probably not working
        if Feed.objects.filter(
            catalog=form.cleaned_data["catalog_id"],
            url_name=form.cleaned_data["url_name"],
        ).exists():
            raise ProblemDetailException(
                _("Feed with same url_name already exists in same catalog"),
                status=HTTPStatus.CONFLICT,
            )

        feed = Feed(creator=request.user)
        form.populate(feed)
        feed.save()

        if "entries" in form.cleaned_data.keys():
            feed.entries.add(*form.cleaned_data["entries"])

        if "parents" in form.cleaned_data.keys():
            feed.parents.add(*form.cleaned_data["parents"])

        return SingleResponse(request, data=FeedSerializer.Base.model_validate(feed), status=HTTPStatus.CREATED)

    @openapi.metadata(description="List Feeds", tags=["Feeds"])
    def get(self, request):
        feeds = FeedFilter(request.GET, queryset=Feed.objects.all(), request=request).qs

        return PaginationResponse(request, feeds, serializer=FeedSerializer.Base)


class FeedDetail(SecuredView):
    @staticmethod
    def _get_feed(request, feed_id: UUID) -> Feed:
        try:
            feed = Feed.objects.select_related("catalog").get(pk=feed_id)
        except Feed.DoesNotExist as e:
            raise ProblemDetailException(_("Feed not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission("check_catalog_read", request.user, feed.catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return feed

    @openapi.metadata(description="Get Feed detail", tags=["Feeds"])
    def get(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id)

        return SingleResponse(request, data=FeedSerializer.Base.model_validate(feed))

    @openapi.metadata(description="Update Feed", tags=["Feeds"])
    def put(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id)

        form = FeedForm.create_from_request(request)
        form["parents"].queryset = form["parents"].queryset.exclude(pk=feed.pk)

        if not form.is_valid():
            raise ValidationException(form)

        if (
            Feed.objects.filter(
                catalog=form.cleaned_data["catalog_id"],
                url_name=form.cleaned_data["url_name"],
            )
            .exclude(pk=feed.id)
            .exists()
        ):
            raise ProblemDetailException(
                _("Feed with same url_name already exists in same catalog"),
                status=HTTPStatus.CONFLICT,
            )

        form.populate(feed)
        feed.save()

        if feed.kind == Feed.FeedKind.ACQUISITION and "entries" in form.cleaned_data.keys():
            feed.entries.clear()
            feed.entries.add(*form.cleaned_data["entries"])

        if "parents" in form.cleaned_data.keys():
            feed.parents.clear()
            feed.parents.add(*form.cleaned_data["parents"])

        return SingleResponse(request, data=FeedSerializer.Base.model_validate(feed))

    @openapi.metadata(description="Delete Feed", tags=["Feeds"])
    def delete(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id)
        feed.delete()

        return SingleResponse(request)
