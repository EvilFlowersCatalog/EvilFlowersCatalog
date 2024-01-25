from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.annotations import AnnotationItemFilter
from apps.api.forms.annotations import (
    AnnotationItemForm,
)
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.annotation import (
    AnnotationItemSerializer,
)
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.models import AnnotationItem
from apps.core.views import SecuredView


class AnnotationItemManagement(SecuredView):
    def get(self, request):
        annotation_items = AnnotationItemFilter(
            request.GET, queryset=AnnotationItem.objects.all(), request=request
        ).qs

        return PaginationResponse(
            request, annotation_items, serializer=AnnotationItemSerializer.Base
        )

    def post(self, request):
        form = AnnotationItemForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        if not has_object_permission(
            "check_user_acquisition_read",
            request.user,
            form.cleaned_data["annotation"].user_acquisition,
        ):
            raise ProblemDetailException(
                _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN
            )

        annotation_item = AnnotationItem()
        form.populate(annotation_item)
        annotation_item.save()

        return SingleResponse(
            request,
            AnnotationItemSerializer.Base.model_validate(annotation_item),
            status=HTTPStatus.CREATED,
        )


class AnnotationItemDetail(SecuredView):
    @staticmethod
    def _get_annotation_item(request, annotation_item_id: UUID) -> AnnotationItem:
        try:
            annotation_item = AnnotationItem.objects.get(pk=annotation_item_id)
        except AnnotationItem.DoesNotExist as e:
            raise ProblemDetailException(
                _("Annotation item not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        if not has_object_permission(
            "check_user_acquisition_read",
            request.user,
            annotation_item.annotation.user_acquisition,
        ):
            raise ProblemDetailException(
                _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN
            )

        return annotation_item

    def get(self, request, annotation_item_id: UUID):
        annotation_item = self._get_annotation_item(request, annotation_item_id)
        return SingleResponse(
            request, AnnotationItemSerializer.Base.model_validate(annotation_item)
        )

    def put(self, request, annotation_item_id: UUID):
        form = AnnotationItemForm.create_from_request(request)
        annotation_item = self._get_annotation_item(request, annotation_item_id)

        if not form.is_valid():
            raise ValidationException(form)

        form.populate(annotation_item)
        annotation_item.save()

        return SingleResponse(
            request, AnnotationItemSerializer.Base.model_validate(annotation_item)
        )

    def delete(self, request, annotation_id: UUID):
        annotation_item = self._get_annotation_item(request, annotation_id)
        annotation_item.delete()

        return SingleResponse(request)
