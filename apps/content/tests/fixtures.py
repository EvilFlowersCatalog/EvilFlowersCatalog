"""The access scenarios the parity tests (#172) and the policy tests (#174) share."""

from datetime import timedelta

from django.test import RequestFactory
from django.utils import timezone

from apps.content import policies
from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User, UserAcquisition, UserCatalog
from apps.readium.models import License

ALLOWED_RANGES = ["10.0.0.0/8"]
INSIDE = "10.1.2.3"
OUTSIDE = "192.0.2.10"


class Scenarios:
    """Entries of every kind the gate tells apart, and a reader who holds various things about them."""

    @classmethod
    def build(cls, target):
        policies.ensure_presets()
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        target.owner = cls._user("owner", source)
        target.reader = cls._user("reader", source)
        target.member = cls._user("member", source)
        target.public = Catalog.objects.create(creator=target.owner, title="Open", url_name="open", is_public=True)
        target.private = Catalog.objects.create(creator=target.owner, title="Private", url_name="private")
        UserCatalog.objects.create(user=target.member, catalog=target.public, mode=UserCatalog.Mode.READ)

        def entry(title, catalog=None, **config):
            return Entry.objects.create(
                creator=target.owner, catalog=catalog or target.public, title=title, config=config
            )

        target.open = entry("Open book")
        target.lcp = entry("Lent book", readium_enabled=True)
        target.lcp_loan = entry("Borrowed book", readium_enabled=True)
        target.lcp_returned = entry("Returned book", readium_enabled=True)
        target.ip = entry("Campus book", evilflowers_ip_block=True)
        target.lcp_ip = entry("Lent campus book", readium_enabled=True, evilflowers_ip_block=True)
        target.hidden = entry("Hidden book", catalog=target.private)
        target.own_copy = entry("Own copy")
        target.own_copy_lcp = entry("Own copy of a lent book", readium_enabled=True)

        now = timezone.now()
        License.objects.create(
            entry=target.lcp_loan,
            user=target.reader,
            state=License.LicenseState.ACTIVE,
            starts_at=now,
            expires_at=now + timedelta(days=7),
        )
        License.objects.create(
            entry=target.lcp_returned,
            user=target.reader,
            state=License.LicenseState.RETURNED,
            starts_at=now - timedelta(days=14),
            expires_at=now - timedelta(days=7),
            device_count=1,
        )
        for book in (target.own_copy, target.own_copy_lcp):
            acquisition = Acquisition.objects.create(entry=book, mime=Acquisition.AcquisitionMIME.PDF)
            UserAcquisition.objects.create(
                acquisition=acquisition, user=target.reader, type=UserAcquisition.UserAcquisitionType.PERSONAL
            )

    @staticmethod
    def _user(username, source):
        user = User(username=username, name=username.title(), surname="Tester", auth_source=source)
        user.set_unusable_password()
        user.save()
        return user


def request_for(user, ip: str = OUTSIDE, api_key=None):
    request = RequestFactory().get("/", REMOTE_ADDR=ip)
    request.user = user
    if api_key is not None:
        request.api_key = api_key
    return request
