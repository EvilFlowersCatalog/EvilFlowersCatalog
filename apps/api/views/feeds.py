from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from django.views.generic.base import View

from apps.api.errors import ValidationException, ApiException
from apps.api.filters.feeds import FeedFilter
from apps.api.forms.feeds import FeedForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.feeds import FeedSerializer
from apps.core.models import Feed


class FeedManagement(View):
    require_apikey = ['post', 'get']

    def post(self, request):
        form = FeedForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if form.cleaned_data['catalog_id'].creator_id != request.api_key.user_id:
            raise ApiException(request, _("Insufficient permissions"), status_code=HTTPStatus.FORBIDDEN)

        if Feed.objects.filter(
            catalog=form.cleaned_data['catalog_id'],
            url_name=form.cleaned_data['url_name']
        ):
            raise ApiException(
                request, _("Feed with same url_name already exists in same catalog"), status_code=HTTPStatus.CONFLICT
            )

        feed = Feed(creator=request.api_key.user)
        form.fill(feed)
        feed.save()

        return SingleResponse(request, feed, serializer=FeedSerializer.Base, status=HTTPStatus.CREATED)

    def get(self, request):
        feeds = FeedFilter(request.GET, queryset=Feed.objects.all(), request=request).qs

        return PaginationResponse(
            request, feeds, page=request.GET.get('page', 1), serializer=FeedSerializer.Base
        )


class FeedDetail(View):
    require_apikey = ['put', 'get', 'delete']

    @staticmethod
    def _get_feed(request, feed_id: UUID) -> Feed:
        try:
            feed = Feed.objects.get(pk=feed_id)
        except Feed.DoesNotExist as e:
            raise ApiException(request, _("Feed not found"), status_code=HTTPStatus.NOT_FOUND, previous=e)

        if not request.api_key.user.is_superuser and feed.creator_id != request.api_key.user_id:
            raise ApiException(request, _("Insufficient permissions"), status_code=HTTPStatus.FORBIDDEN)

        return feed

    def get(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id)

        return SingleResponse(request, feed, serializer=FeedSerializer.Base)

    def put(self, request, feed_id: UUID):
        form = FeedForm.create_from_request(request)

        feed = self._get_feed(request, feed_id)

        if not form.is_valid():
            raise ValidationException(request, form)

        if Feed.objects.filter(
            catalog=form.cleaned_data['catalog_id'],
            url_name=form.cleaned_data['url_name']
        ).exclude(pk=feed.id).exists():
            raise ApiException(
                request, _("Feed with same url_name already exists in same catalog"), status_code=HTTPStatus.CONFLICT
            )

        form.fill(feed)
        feed.save()

        return SingleResponse(request, feed, serializer=FeedSerializer.Base)

    def delete(self, request, feed_id: UUID):
        feed = self._get_feed(request, feed_id)
        feed.hard_delete()

        return SingleResponse(request)
