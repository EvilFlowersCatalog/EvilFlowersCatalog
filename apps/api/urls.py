from django.urls import path

from apps.api.views import (
    catalogs,
    feeds,
    entries,
    api_keys,
    users,
    acquisitions,
    authors,
    status,
    tokens,
    user_acquisitions,
    annotations,
    shelf_records,
    user_activities,
    categories,
    annotation_items,
    languages,
    notification_contacts,
    reviews,
    jobs,
    instance_settings,
)

# IP-008 Phase 4: the Dataverse integration moved to `apps.dataverse`.
# The legacy `/api/v1/dataverse-prepublish` and `/api/v1/dataverse-sync`
# routes remain wired here as one-release shims so Dataverse workflows
# that hardcoded the path keep working. The `DataverseSync` route was
# documented but never called by any workflow — kept as a 200 stub for
# observability so an unexpected POST is visible in logs rather than
# silently 404ing.
from apps.content import views as content_views
from apps.dataverse.views import PrepublishView as _DataversePrepublishView
from apps.decider import views as decider_views
from apps.extraction import admin_views as extraction_admin
from apps.extraction import discovery as extraction_discovery
from apps.extraction import figure_search as extraction_figure_search
from apps.extraction import locator_views as extraction_locators
from apps.extraction import views as extraction_views
from apps.graph import views as graph_views
from apps.readium.views.statistics import StatisticsView

urlpatterns = [
    # Instance settings (#20)
    path("settings", instance_settings.SettingCollection.as_view(), name="setting-collection"),
    path("settings/<str:key>", instance_settings.SettingDetail.as_view(), name="setting-detail"),
    # Content policies (#174); the fixed paths before `<key>`.
    path("cite", content_views.CiteReferences.as_view(), name="cite"),
    path("content-policies", content_views.ContentPolicyCollection.as_view(), name="content-policy-collection"),
    path("content-policies/schema", content_views.ContentPolicySchema.as_view(), name="content-policy-schema"),
    path("content-policies/explain", content_views.ContentPolicyExplain.as_view(), name="content-policy-explain"),
    path("content-policies/<str:key>", content_views.ContentPolicyDetail.as_view(), name="content-policy-detail"),
    path(
        "content-policies/<str:key>/versions",
        content_views.ContentPolicyVersions.as_view(),
        name="content-policy-versions",
    ),
    path(
        "content-policies/<str:key>/assign", content_views.ContentPolicyAssign.as_view(), name="content-policy-assign"
    ),
    # API keys
    path("api_keys", api_keys.ApiKeyManagement.as_view()),
    path("api_keys/<uuid:api_key_id>", api_keys.ApiKeyDetail.as_view()),
    # Users
    path("users", users.UserManagement.as_view()),
    path("users/me", users.UserMe.as_view()),
    path("users/me/password", users.UserPassword.as_view(), name="user-password"),
    path("users/<uuid:user_id>", users.UserDetail.as_view()),
    # Catalogs
    path("catalogs", catalogs.CatalogManagement.as_view()),
    path("catalogs/<uuid:catalog_id>", catalogs.CatalogDetail.as_view()),
    # Feeds
    path("feeds", feeds.FeedManagement.as_view()),
    path("feeds/<uuid:feed_id>", feeds.FeedDetail.as_view()),
    # Authors
    path("authors", authors.AuthorManagement.as_view()),
    path("authors/<uuid:author_id>", authors.AuthorDetail.as_view()),
    # Categories
    path(
        "categories",
        categories.CategoryManagement.as_view(),
        name="category-management",
    ),
    path(
        "categories/<uuid:category_id>",
        categories.CategoryDetail.as_view(),
        name="category-detail",
    ),
    # Entries
    path("entries", entries.EntryPaginator.as_view()),
    path("entries/facets", entries.EntryFacets.as_view()),
    path("entries/suggest", entries.EntrySuggest.as_view()),
    path("entries/passages", extraction_discovery.EntryPassages.as_view(), name="entry-passages"),
    # Figures and tables across the library (#159)
    path("search", extraction_figure_search.FigureSearch.as_view(), name="figure-search"),
    # The library graph and the vectors behind it, read-only for the portal (#152)
    path("entries/map", graph_views.LibraryMap.as_view(), name="entry-map"),
    path("entries/<uuid:entry_id>/chapters", graph_views.EntryChapters.as_view(), name="entry-chapters"),
    path("sections/<str:key>/related", graph_views.SectionRelated.as_view(), name="section-related"),
    path("sections/<str:key>/prerequisites", graph_views.SectionPrerequisites.as_view(), name="section-prerequisites"),
    path("sections/<str:key>/examples", graph_views.SectionExamples.as_view(), name="section-examples"),
    path("concepts", graph_views.ConceptList.as_view(), name="concept-list"),
    path("concepts/<str:key>", graph_views.ConceptDetail.as_view(), name="concept-detail"),
    path("graph/overview", graph_views.GraphOverview.as_view(), name="graph-overview"),
    # Labelling the decision model's calibration cases (#201)
    path("sections/<str:key>/text", decider_views.SectionText.as_view(), name="section-text"),
    path("decider/section-pairs", decider_views.SectionPairs.as_view(), name="decider-section-pairs"),
    # Run-independent addresses of paragraphs, sections and figures (#171)
    path("locators/<uuid:entry_id>/<str:key>", extraction_locators.EntryLocator.as_view(), name="entry-locator"),
    # Recommendations and figures from the extraction index (IP-018 Phase 8)
    path("entries/<uuid:entry_id>/similar", extraction_discovery.EntrySimilar.as_view(), name="entry-similar"),
    # IP-023: ratings and reviews
    path("entries/<uuid:entry_id>/reviews", reviews.EntryReviewList.as_view(), name="entry-reviews"),
    path("entries/<uuid:entry_id>/reviews/mine", reviews.OwnEntryReview.as_view(), name="entry-review-mine"),
    path("reviews", reviews.ReviewList.as_view(), name="review-list"),
    path("reviews/<uuid:review_id>", reviews.EntryReviewDetail.as_view(), name="review-detail"),
    path("entries/<uuid:entry_id>/figures", extraction_discovery.EntryFigures.as_view(), name="entry-figures"),
    # Extraction for the entry's managers: status, runs, queue, (re)processing (IP-018 admin)
    path("entries/<uuid:entry_id>/extraction", extraction_admin.EntryExtraction.as_view(), name="entry-extraction"),
    path(
        "entries/<uuid:entry_id>/extraction/regions",
        extraction_admin.EntryExtractionRegions.as_view(),
        name="entry-extraction-regions",
    ),
    path(
        "entries/<uuid:entry_id>/extraction/regions/<uuid:region_id>/image",
        extraction_admin.EntryExtractionRegionImage.as_view(),
        name="entry-extraction-region-image",
    ),
    path(
        "entries/<uuid:entry_id>/extraction/paragraphs",
        extraction_admin.EntryExtractionParagraphs.as_view(),
        name="entry-extraction-paragraphs",
    ),
    path("entry-introspection", entries.EntryIntrospection.as_view()),
    path("catalogs/<uuid:catalog_id>/entries", entries.EntryManagement.as_view()),
    path(
        "catalogs/<uuid:catalog_id>/entries/<uuid:entry_id>",
        entries.EntryDetail.as_view(),
    ),
    # Acquisitions
    path(
        "acquisitions",
        acquisitions.AcquisitionManagement.as_view(),
        name="acquisition-management",
    ),
    path(
        "acquisitions/<uuid:acquisition_id>",
        acquisitions.AcquisitionDetail.as_view(),
        name="acquisition-detail",
    ),
    # Shelves
    path(
        "shelf-records",
        shelf_records.ShelfRecordManagement.as_view(),
        name="shelf-record-management",
    ),
    path(
        "shelf-records/<uuid:shelf_record_id>",
        shelf_records.ShelfRecordDetail.as_view(),
        name="shelf-record-detail",
    ),
    # Personal activity history (IP-016)
    path(
        "activity",
        user_activities.UserActivityManagement.as_view(),
        name="user-activity-management",
    ),
    # User acquisitions
    path(
        "user-acquisitions",
        user_acquisitions.UserAcquisitionManagement.as_view(),
        name="user-acquisition-management",
    ),
    path(
        "user-acquisitions/<uuid:user_acquisition_id>",
        user_acquisitions.UserAcquisitionDetail.as_view(),
        name="user-acquisition-detail",
    ),
    # Extraction (IP-018): regions of a reader's copy, in the reader's page numbers
    path(
        "user-acquisitions/<uuid:user_acquisition_id>/extraction",
        extraction_views.UserAcquisitionExtraction.as_view(),
        name="user-acquisition-extraction",
    ),
    path(
        "user-acquisitions/<uuid:user_acquisition_id>/similar",
        extraction_discovery.UserAcquisitionSimilar.as_view(),
        name="user-acquisition-similar",
    ),
    path(
        "user-acquisitions/<uuid:user_acquisition_id>/regions",
        extraction_views.UserAcquisitionRegions.as_view(),
        name="user-acquisition-regions",
    ),
    path(
        "user-acquisitions/<uuid:user_acquisition_id>/regions/<uuid:region_id>",
        extraction_views.UserAcquisitionRegionDetail.as_view(),
        name="user-acquisition-region-detail",
    ),
    # Annotations
    path(
        "annotations",
        annotations.AnnotationManagement.as_view(),
        name="annotation-management",
    ),
    path(
        "annotations/<uuid:annotation_id>",
        annotations.AnnotationDetail.as_view(),
        name="annotation-detail",
    ),
    path(
        "annotation-items",
        annotation_items.AnnotationItemManagement.as_view(),
        name="annotation-item-management",
    ),
    path(
        "annotation-items/<uuid:annotation_item_id>",
        annotation_items.AnnotationItemDetail.as_view(),
        name="annotation-item-detail",
    ),
    # Notification contacts
    path(
        "notification-contacts",
        notification_contacts.NotificationContactManagement.as_view(),
        name="notification-contact-management",
    ),
    path(
        "notification-contacts/<uuid:contact_id>",
        notification_contacts.NotificationContactDetail.as_view(),
        name="notification-contact-detail",
    ),
    # Background jobs (IP-031)
    path("jobs", jobs.JobCollection.as_view(), name="job-collection"),
    path("jobs/kinds", jobs.JobKinds.as_view(), name="job-kinds"),
    path("jobs/summary", jobs.JobSummary.as_view(), name="job-summary"),
    path("jobs/runs", jobs.RunCollection.as_view(), name="job-run-collection"),
    path("jobs/<uuid:job_id>", jobs.JobDetail.as_view(), name="job-detail"),
    # Status
    path("status", status.StatusManagement.as_view(), name="status"),
    # GitHub #131: admin dashboard statistics
    path("statistics", StatisticsView.as_view(), name="statistics"),
    # Tokens
    path("token/refresh", tokens.RefreshTokenManagement.as_view(), name="refresh"),
    path("token", tokens.AccessTokenManagement.as_view(), name="login"),
    path("languages", languages.LanguageManagement.as_view(), name="languages"),
    # Dataverse (legacy shim path — implementation now in apps.dataverse).
    # `dataverse-sync` was never called by any Dataverse workflow; we used to
    # 200-OK the request. Drop the route — a 404 here surfaces the dead
    # contract in logs. If a workflow ever sends one, we'll know.
    path("dataverse-prepublish", _DataversePrepublishView.as_view(), name="dataverse-prepublish"),
]
