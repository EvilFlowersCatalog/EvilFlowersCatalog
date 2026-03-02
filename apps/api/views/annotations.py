from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.annotations import AnnotationFilter
from apps.api.forms.annotations import CreateAnnotationForm, UpdateAnnotationForm
from apps.api.response import PaginationResponse, SingleResponse
from apps.api.serializers.annotation import AnnotationSerializer
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.core.models import Annotation
from apps.core.views import SecuredView


class AnnotationManagement(SecuredView):
    @openapi.metadata(
        description="Retrieve a paginated list of annotations created by users. Annotations include highlights, notes, bookmarks, and other user-generated content associated with entries. Supports filtering by user, entry, and annotation type.",
        tags=["Annotations"],
        summary="List user annotations",
    )
    def get(self, request):
        annotations = AnnotationFilter(request.GET, queryset=Annotation.objects.all(), request=request).qs

        return PaginationResponse(request, annotations, serializer=AnnotationSerializer.Base)

    @openapi.metadata(
        description="Create a new annotation for an entry. Annotations can be highlights, notes, bookmarks, or other user-generated content that enhances the reading experience. Each annotation is associated with a specific location within the entry.",
        tags=["Annotations"],
        summary="Create annotation",
    )
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
            data=AnnotationSerializer.Base.model_validate(annotation),
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

    @openapi.metadata(
        description="Retrieve detailed information about a specific annotation including its content, position, type, and associated entry information.",
        tags=["Annotations"],
        summary="Get annotation details",
    )
    def get(self, request, annotation_id: UUID):
        annotation = self._get_annotation(request, annotation_id)
        return SingleResponse(request, data=AnnotationSerializer.Base.model_validate(annotation))

    @openapi.metadata(
        description="Update an existing annotation's content, position, or type. Only the annotation creator can modify their annotations. This allows users to edit their notes, adjust highlights, or change annotation properties.",
        tags=["Annotations"],
        summary="Update annotation",
    )
    def put(self, request, annotation_id: UUID):
        form = UpdateAnnotationForm.create_from_request(request)
        annotation = self._get_annotation(request, annotation_id)

        if not form.is_valid():
            raise ValidationException(form)

        form.populate(annotation)
        annotation.save()

        return SingleResponse(request, data=AnnotationSerializer.Base.model_validate(annotation))

    @openapi.metadata(
        description="Permanently delete an annotation. Only the annotation creator can delete their annotations. This action is irreversible and will remove all associated data.",
        tags=["Annotations"],
        summary="Delete annotation",
    )
    def delete(self, request, annotation_id: UUID):
        annotation = self._get_annotation(request, annotation_id)
        annotation.delete()

        return SingleResponse(request)
