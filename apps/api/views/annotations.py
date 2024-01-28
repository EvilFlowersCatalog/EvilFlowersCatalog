from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.filters.annotations import AnnotationFilter
from apps.api.forms.annotations import CreateAnnotationForm, UpdateAnnotationForm
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.annotation import AnnotationSerializer
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.models import Annotation
from apps.core.views import SecuredView


class AnnotationManagement(SecuredView):
    def get(self, request):
        annotations = AnnotationFilter(request.GET, queryset=Annotation.objects.all(), request=request).qs

        return PaginationResponse(request, annotations, serializer=AnnotationSerializer.Base)

    def post(self, request):
        form = CreateAnnotationForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        if not has_object_permission(
            "check_user_acquisition_read",
            request.user,
            form.cleaned_data["user_acquisition_id"],
        ):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        annotation = Annotation()
        form.populate(annotation)
        annotation.save()

        return SingleResponse(
            request,
            AnnotationSerializer.Base.model_validate(annotation),
            status=HTTPStatus.CREATED,
        )


class AnnotationDetail(SecuredView):
    @staticmethod
    def _get_annotation(request, annotation_id: UUID) -> Annotation:
        try:
            annotation = Annotation.objects.get(pk=annotation_id)
        except Annotation.DoesNotExist as e:
            raise ProblemDetailException(
                _("Annotation not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
                detail_type=DetailType.NOT_FOUND,
            )

        if not has_object_permission("check_user_acquisition_read", request.user, annotation.user_acquisition):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return annotation

    def get(self, request, annotation_id: UUID):
        annotation = self._get_annotation(request, annotation_id)
        return SingleResponse(request, AnnotationSerializer.Base.model_validate(annotation))

    def put(self, request, annotation_id: UUID):
        form = UpdateAnnotationForm.create_from_request(request)
        annotation = self._get_annotation(request, annotation_id)

        if not form.is_valid():
            raise ValidationException(form)

        form.populate(annotation)
        annotation.save()

        return SingleResponse(request, AnnotationSerializer.Base.model_validate(annotation))

    def delete(self, request, annotation_id: UUID):
        annotation = self._get_annotation(request, annotation_id)
        annotation.delete()

        return SingleResponse(request)
