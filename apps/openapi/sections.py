"""The sections of the API reference: every tag, its description, its group, and where untagged routes belong.

Redoc renders `tags` as the sections of the sidebar and `x-tagGroups` as the headings above them. A tag
that is used by an operation but missing from every group disappears from the sidebar together with its
operations, so `OpenApiDocument` refuses to build a document that uses an undeclared tag (see tests).

Views name their section with `@openapi.metadata(tags=[...])`. Routes that cannot or do not (webhooks,
protocol documents, well-known files) are filed by `PATH_RULES`, first match wins.
"""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TagSpec:
    name: str
    description: str
    external_docs: tuple[str, str] | None = None  # (url, description)


@dataclass(frozen=True)
class TagGroup:
    name: str
    tags: list[TagSpec] = field(default_factory=list)


@dataclass(frozen=True)
class PathRule:
    """Defaults for the operations of every route whose OpenAPI path matches `pattern`."""

    pattern: str
    tag: str
    # Media type of a response the analyzer could not describe (protocol documents, HTML pages).
    media_type: str | None = None
    # Prefix of the operationId, for protocol surfaces whose view names repeat across apps (`FeedView`).
    operation_prefix: str = ""

    def matches(self, path: str) -> bool:
        return re.search(self.pattern, path) is not None


GROUPS: list[TagGroup] = [
    TagGroup(
        "Library",
        [
            TagSpec(
                "Catalogs",
                "Catalogs are the tenants of the library. Every publication, feed and category lives in exactly one "
                "catalog, and users are granted access (`read`, `write`, `manage`) per catalog.",
            ),
            TagSpec(
                "Entries",
                "Publications: their metadata, search, facets, suggestions and similarity, and the reading structure "
                "(chapters, figures, locators) built from their content.",
            ),
            TagSpec("Authors", "People credited on publications, shared by the entries that name them."),
            TagSpec(
                "Categories",
                "The subject vocabulary of a catalog (for example its MDT/UDC classes). Entries are filed under the "
                "categories of their own catalog.",
            ),
            TagSpec(
                "Feeds",
                "Curated, ordered lists of entries. Every feed is published as an OPDS navigation or acquisition feed.",
            ),
            TagSpec("Acquisitions", "The files of an entry (PDF, EPUB, …) and their metadata."),
            TagSpec(
                "Files",
                "Binary downloads: acquisition files, a reader's personalised copy, covers, thumbnails and the images "
                "of extracted regions.",
            ),
            TagSpec("Reviews", "Ratings and reviews of entries by readers, and their moderation."),
            TagSpec(
                "Graph",
                "The knowledge graph built from publication content: related sections, prerequisites and concepts.",
            ),
        ],
    ),
    TagGroup(
        "Reading",
        [
            TagSpec(
                "User Acquisitions",
                "A reader's own copy of an acquisition (for example a personalised PDF) and what it lets them do.",
            ),
            TagSpec("Shelf Records", "Entries a reader keeps on their shelf."),
            TagSpec("Annotations", "A reader's annotation sets on a publication."),
            TagSpec("Annotation Items", "The highlights and notes inside an annotation set."),
            TagSpec("Activity", "A reader's own reading and lending history."),
        ],
    ),
    TagGroup(
        "Lending (Readium LCP)",
        [
            TagSpec(
                "Licenses",
                "Readium LCP licenses: issue and inspect them, download the `.lcpl`, and the License Status Document "
                "interactions reading apps perform (status, register, renew, return).",
                ("https://readium.org/lcp-specs/releases/lsd/latest", "License Status Document specification"),
            ),
            TagSpec(
                "Reservations",
                "The queue for LCP titles without a free copy, including the claim page linked from the notification "
                "e-mail.",
            ),
            TagSpec(
                "Readium",
                "Readium integration around a publication: availability, the Readium Web Publication Manifest, the "
                "encrypted content reading apps download and the passphrase hint page.",
                ("https://readium.org/architecture/", "Readium architecture"),
            ),
            TagSpec(
                "Encryption",
                "LCP encryption of publications and the callbacks of the encryption worker.",
            ),
        ],
    ),
    TagGroup(
        "Content and AI",
        [
            TagSpec("Content", "Working with publication content: formatted references and citations."),
            TagSpec(
                "Content policies",
                "Versioned policies that decide how much of a publication's text a caller may read, quote or export.",
            ),
            TagSpec(
                "Extraction",
                "Text, region and paragraph extraction of publications, and the endpoints the extraction workers "
                "report to.",
            ),
            TagSpec(
                "Decider",
                "The decision model that types relations between sections and decides what readers are shown, with "
                "the endpoints used to label its calibration cases.",
            ),
            TagSpec("Assistant", "The library assistant: chats grounded in the catalog, explanations and highlights."),
        ],
    ),
    TagGroup(
        "Interoperability",
        [
            TagSpec(
                "OPDS 1.2",
                "Atom-based OPDS 1.2 catalog feeds for reading apps: navigation, acquisition and search.",
                ("https://specs.opds.io/opds-1.2", "OPDS 1.2 specification"),
            ),
            TagSpec(
                "OPDS 2.0",
                "JSON-based OPDS 2.0 feeds, publications, authentication documents and borrowing.",
                ("https://drafts.opds.io/opds-2.0", "OPDS 2.0 specification"),
            ),
            TagSpec(
                "OAI-PMH",
                "The OAI-PMH 2.0 provider for library aggregators, the ISBN resolver and availability lookups.",
                ("https://www.openarchives.org/OAI/openarchivesprotocol.html", "OAI-PMH 2.0 specification"),
            ),
            TagSpec(
                "Harvest",
                "The harvesting client: external sources, their records, conflicts and the sync state of an entry.",
            ),
            TagSpec("Dataverse", "The hooks a Dataverse installation calls before it publishes a dataset."),
            TagSpec(
                "MCP",
                "The Model Context Protocol server for AI clients, its OAuth protected-resource metadata and the "
                "signed upload links its tools hand out.",
                ("https://modelcontextprotocol.io/specification", "Model Context Protocol specification"),
            ),
            TagSpec(
                "Discovery",
                "Well-known documents machines read without credentials, such as the W3C TDMRep reservations.",
                ("https://www.w3.org/community/tdmrep/", "W3C TDM Reservation Protocol"),
            ),
        ],
    ),
    TagGroup(
        "Accounts",
        [
            TagSpec("Users", "User accounts, the caller's own profile and password."),
            TagSpec("Tokens", "Issue and refresh the JWT access tokens the API accepts as `Bearer` credentials."),
            TagSpec("API Keys", "Long-lived API keys for scripts and integrations."),
            TagSpec(
                "Notification Contacts", "Where a user wants to be notified (e-mail addresses and other channels)."
            ),
            TagSpec("Notifications", "Assets embedded in notification e-mails, such as the institution's logo."),
        ],
    ),
    TagGroup(
        "Administration",
        [
            TagSpec("Settings", "Instance settings an administrator can change at runtime."),
            TagSpec("Jobs", "Background jobs: what can run, what is running, and the runs' history."),
            TagSpec("Status", "Health and version of the instance and the services it depends on."),
            TagSpec("Statistics", "Lending statistics of the catalogs."),
            TagSpec("Enums", "Enumerations clients render as choices, such as languages."),
        ],
    ),
]


PATH_RULES: list[PathRule] = [
    # Readium: callbacks of the encryption worker, the LSD proxy and reader-facing pages.
    PathRule(r"^/readium/v1/hooks/", "Encryption"),
    PathRule(r"^/readium/v1/licenses/\{[^}]+\}\.lcpl$", "Licenses", "application/vnd.readium.lcp.license.v1.0+json"),
    PathRule(
        r"^/readium/v1/licenses/\{[^}]+\}/(status|register|return|renew)$",
        "Licenses",
        media_type="application/vnd.readium.license.status.v1.0+json",
    ),
    PathRule(r"^/readium/v1/licenses", "Licenses"),
    PathRule(r"^/readium/v1/reservations/\{[^}]+\}/claim$", "Reservations", media_type="text/html"),
    PathRule(r"^/readium/v1/publications/", "Readium", media_type="application/webpub+json"),
    PathRule(r"^/readium/v1/content/", "Readium", media_type="application/octet-stream"),
    PathRule(r"^/readium/v1/hint$", "Readium", media_type="text/html"),
    PathRule(r"^/readium/v1/", "Readium"),
    # Protocol surfaces.
    PathRule(r"^/opds/v1\.2/", "OPDS 1.2", media_type="application/atom+xml", operation_prefix="opds1."),
    PathRule(r"^/opds/v2/", "OPDS 2.0", media_type="application/opds+json", operation_prefix="opds2."),
    PathRule(r"^/oai$", "OAI-PMH", media_type="text/xml"),
    PathRule(r"^/resolve/isbn/", "OAI-PMH"),
    PathRule(r"^/availability", "OAI-PMH"),
    PathRule(r"^/resolve/", "Harvest"),
    PathRule(r"^/mcp/v1", "MCP"),
    PathRule(r"^/\.well-known/oauth-protected-resource", "MCP"),
    PathRule(r"^/\.well-known/", "Discovery"),
    PathRule(r"^/notifications/", "Notifications", media_type="image/*"),
    PathRule(r"^/api/v1/dataverse-", "Dataverse"),
    PathRule(r"^/extraction/v1/", "Extraction"),
    # Fallback media types for tagged views whose answer the analyzer cannot see.
    PathRule(r"^/assistant/v1/chats/\{[^}]+\}/messages$", "Assistant", media_type="text/event-stream"),
    PathRule(r"^/assistant/v1/", "Assistant"),
    PathRule(r"/image$", "Files", media_type="image/*"),
    PathRule(r"^/data/v1/", "Files", media_type="application/octet-stream"),
]


#: Sidebar labels for operations whose views carry no `@openapi.metadata(summary=...)`, keyed by operationId.
#: Mostly protocol endpoints, whose behaviour the protocol's specification defines; a test keeps every key live.
SUMMARIES: dict[str, str] = {
    # OPDS 1.2
    "opds1.RootView.get": "Catalog root (navigation feed)",
    "opds1.CompleteFeedView.get": "Complete acquisition feed",
    "opds1.EntryView.get": "Complete entry",
    "opds1.FeedView.get": "Curated feed",
    "opds1.LatestFeedView.get": "Newest entries",
    "opds1.PopularFeedView.get": "Popular entries",
    "opds1.SearchView.get": "Search",
    "opds1.SearchDescriptorView.get": "OpenSearch description",
    "opds1.ShelfFeedView.get": "Reader's shelf",
    # OPDS 2.0
    "opds2.AuthenticationDocumentView.get": "Authentication document",
    "opds2.CatalogView.get": "Catalog root",
    "opds2.CatalogAuthenticationDocumentView.get": "Catalog authentication document",
    "opds2.FeedView.get": "Curated feed",
    "opds2.NavigationView.get": "Navigation",
    "opds2.NewView.get": "Newest publications",
    "opds2.PopularView.get": "Popular publications",
    "opds2.PublicationFeedView.get": "List publications",
    "opds2.PublicationDetailView.get": "Get a publication",
    "opds2.PublicationManifestView.get": "Publication manifest",
    "opds2.BorrowView.get": "Get the loan of a publication",
    "opds2.BorrowView.post": "Borrow a publication",
    "opds2.RenewView.post": "Renew a loan",
    "opds2.ReturnView.post": "Return a loan",
    "opds2.SearchView.get": "Search publications",
    "opds2.ShelfView.get": "Reader's shelf",
    # Readium LCP
    "EncryptionWebhook.post": "Encryption notification (lcpencrypt)",
    "EncryptionResultWebhook.post": "Encryption result (worker)",
    "EncryptedContentDownloadView.get": "Download encrypted content",
    "StatusDocumentView.get": "License Status Document",
    "DeviceRegistrationProxyView.post": "Register a device",
    "ReturnProxyView.put": "Return a loan (LSD)",
    "RenewProxyView.put": "Renew a loan (LSD)",
    "ReservationClaimPage.get": "Reservation claim page",
    "ReservationClaimPage.post": "Claim a reservation",
    "PublicationManifestView.get": "Readium Web Publication Manifest",
    "HintPageView.get": "Passphrase hint page",
    # MCP, discovery, notifications
    "McpEndpoint.post": "Send JSON-RPC messages",
    "McpEndpoint.get": "Open an SSE stream (not supported)",
    "McpEndpoint.delete": "End a session (not supported)",
    "McpUploadEndpoint.post": "Upload a file to a signed link",
    "ProtectedResourceMetadata.get": "OAuth protected-resource metadata",
    "ProtectedResourceMetadata.get_2": "OAuth protected-resource metadata (root)",
    "TDMRepView.get": "TDM reservations (TDMRep)",
    "NotificationAsset.get": "Notification e-mail asset",
    # Interoperability
    "ResolveView.get": "Resolve a harvested record",
    # Files
    "EntryExtractionRegionImage.get": "Download a region image of an entry",
    "AcquisitionDownload.get": "Download an acquisition",
    "UserAcquisitionDownload.get": "Download a reader's copy",
    "UserAcquisitionRegionImage.get": "Download a region image of a reader's copy",
    "EntryRegionImage.get": "Download a region image",
    "EntryImageDownload.get": "Download a cover",
    "EntryThumbnailDownload.get": "Download a thumbnail",
    # Annotation items
    "AnnotationItemManagement.get": "List annotation items",
    "AnnotationItemManagement.post": "Create an annotation item",
    "AnnotationItemDetail.get": "Get an annotation item",
    "AnnotationItemDetail.put": "Update an annotation item",
    "AnnotationItemDetail.delete": "Delete an annotation item",
}


def all_tags() -> dict[str, TagSpec]:
    return {tag.name: tag for group in GROUPS for tag in group.tags}


def rule_for(path: str) -> PathRule | None:
    for rule in PATH_RULES:
        if rule.matches(path):
            return rule
    return None
