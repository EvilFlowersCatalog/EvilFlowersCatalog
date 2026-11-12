"""`GET /api/v1/statistics` — admin dashboard statistics (GitHub #131)."""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.core.models import Acquisition, AuthSource, Catalog, Category, Entry, User, UserCatalog
from apps.core.views import SecuredView
from apps.readium.models import EncryptedContent, License, Reservation
from apps.readium.services.statistics import TITLES_LIMIT, build_statistics

TZ = "Europe/Bratislava"
LOCAL = ZoneInfo(TZ)
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
# Well after March 2026, so every fixture loan has ended.
NOW = datetime(2026, 4, 20, 12, 0, tzinfo=dt_timezone.utc)


def local(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=LOCAL)


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None, CACHES=LOCMEM_CACHE)
class StatisticsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = cls._user("admin", superuser=True)
        cls.manager = cls._user("manager")
        cls.reader = cls._user("reader")
        cls.other_manager = cls._user("other")
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Lending", url_name="lending")
        cls.other_catalog = Catalog.objects.create(creator=cls.admin, title="Other", url_name="other")
        UserCatalog.objects.create(user=cls.manager, catalog=cls.catalog, mode=UserCatalog.Mode.MANAGE)
        UserCatalog.objects.create(user=cls.reader, catalog=cls.catalog, mode=UserCatalog.Mode.READ)
        UserCatalog.objects.create(user=cls.other_manager, catalog=cls.other_catalog, mode=UserCatalog.Mode.MANAGE)

    @classmethod
    def _user(cls, username, superuser=False) -> User:
        user = User(username=username, name="N", surname="S", auth_source=cls.auth_source, is_superuser=superuser)
        user.set_unusable_password()
        user.save()
        return user

    def setUp(self):
        cache.clear()

    def entry(self, title="Protected", copies=2, registered=True, catalog=None) -> Entry:
        entry = Entry.objects.create(
            creator=self.admin,
            catalog=catalog or self.catalog,
            title=title,
            config={"readium_enabled": True, "readium_amount": copies},
        )
        acquisition = Acquisition.objects.create(entry=entry, mime="application/pdf")
        if registered:
            EncryptedContent.objects.create(
                acquisition=acquisition,
                lcp_content_id=f"content-{entry.pk}",
                encrypted_path="x.lcpdf",
                status=EncryptedContent.EncryptionStatus.REGISTERED,
            )
        return entry

    def license(self, entry, user, state, created, expires, updated=None) -> License:
        lic = License.objects.create(
            entry=entry, user=user, state=state, created_at=created, starts_at=created, expires_at=expires
        )
        License.objects.filter(pk=lic.pk).update(updated_at=updated or created)
        return lic

    def reservation(self, entry, user, status, requested, available=None, updated=None, deadline=None):
        res = Reservation.objects.create(
            entry=entry,
            user=user,
            position=1,
            status=status,
            requested_at=requested,
            available_at=available,
            claim_deadline=deadline,
        )
        Reservation.objects.filter(pk=res.pk).update(updated_at=updated or requested)
        return res

    def build(self, date_from=date(2026, 3, 1), date_to=date(2026, 3, 30), bucket="day", now=NOW, catalog=None):
        return build_statistics((catalog or self.catalog).pk, date_from, date_to, bucket, TZ, now=now)


class StatisticsComputationTests(StatisticsTestCase):
    def setUp(self):
        super().setUp()
        self.book = self.entry()
        # 2 copies over 30 days; one 15-day loan → utilization 0.25.
        self.license(self.book, self.reader, License.LicenseState.EXPIRED, local(2026, 3, 1), local(2026, 3, 16))
        self.reservation(
            self.book,
            self.manager,
            Reservation.Status.CLAIMED,
            requested=local(2026, 3, 2),
            available=local(2026, 3, 5),
            updated=local(2026, 3, 5),
        )
        self.reservation(
            self.book, self.admin, Reservation.Status.EXPIRED, requested=local(2026, 3, 3), updated=local(2026, 3, 10)
        )
        self.reservation(self.book, self.other_manager, Reservation.Status.QUEUED, requested=local(2026, 3, 20))

    def test_hand_computed_title(self):
        stats = self.build()
        (title,) = stats.titles
        self.assertEqual(title.copies, 2)
        self.assertEqual(title.loans, 1)
        self.assertEqual(title.loan_days, 15)
        self.assertEqual(title.utilization, 0.25)
        self.assertEqual(title.queued, 1)
        self.assertEqual(title.in_use, 0)
        self.assertEqual(title.holds_ratio, 0.5)
        self.assertEqual(title.turnover, 0.5)
        self.assertEqual(title.avg_loan_days, 15)
        self.assertIsNotNone(title.last_loan_at)

        self.assertEqual(stats.kpis.utilization.value, 0.25)
        self.assertEqual(stats.kpis.loans.value, 1)
        self.assertEqual(stats.kpis.active_readers.value, 1)
        self.assertEqual(stats.kpis.hold_fulfillment.value, 0.5)
        self.assertEqual(stats.kpis.median_wait_hours.value, 72)
        self.assertEqual(stats.kpis.copies_total, 2)
        self.assertEqual(stats.kpis.lcp_titles, 1)
        self.assertEqual(len(stats.kpis.utilization.trend), 30)

    def test_outcomes_sum_to_totals(self):
        stats = self.build()
        holds = stats.hold_outcomes
        self.assertEqual((holds.placed, holds.claimed, holds.expired_unclaimed, holds.still_waiting), (3, 1, 1, 1))
        self.assertEqual(holds.claimed + holds.expired_unclaimed + holds.cancelled + holds.still_waiting, holds.placed)
        loans = stats.loan_outcomes
        self.assertEqual(sum(loans.model_dump().values()), stats.kpis.loans.value)
        self.assertEqual(loans.ran_to_expiry, 1)

    def test_previous_period(self):
        stats = self.build(date_from=date(2026, 3, 16), date_to=date(2026, 3, 30))
        self.assertEqual(stats.period.previous_from, "2026-03-01")
        self.assertEqual(stats.period.previous_to, "2026-03-15")
        self.assertEqual(stats.kpis.loans.value, 0)
        self.assertEqual(stats.kpis.loans.previous, 1)
        # The 15-day loan fills one of two copies for the whole 15-day previous period.
        self.assertEqual(stats.kpis.utilization.previous, 0.5)
        self.assertEqual(stats.kpis.utilization.value, 0)

    def test_constant_number_of_queries(self):
        for i in range(5):
            entry = self.entry(title=f"Extra {i}")
            self.license(entry, self.reader, License.LicenseState.RETURNED, local(2026, 3, 5), local(2026, 3, 9))
        with self.assertNumQueries(6):
            self.build()

    def test_returned_loan_ends_at_return(self):
        entry = self.entry(title="Returned", copies=1)
        # Returned after 3 days without `expires_at` being moved: `updated_at` is the end.
        self.license(
            entry,
            self.reader,
            License.LicenseState.RETURNED,
            local(2026, 3, 1),
            local(2026, 3, 29),
            updated=local(2026, 3, 4),
        )
        title = next(t for t in self.build().titles if t.title == "Returned")
        self.assertEqual(title.loan_days, 3)
        self.assertEqual(title.avg_loan_days, 3)

    def test_attention(self):
        busy = self.entry(title="Busy", copies=1)
        now = local(2026, 3, 20, 12)
        self.license(busy, self.reader, License.LicenseState.ACTIVE, local(2026, 3, 19), local(2026, 4, 2))
        self.reservation(busy, self.manager, Reservation.Status.QUEUED, requested=local(2026, 3, 19))
        claim = self.entry(title="Claim", copies=1)
        self.reservation(
            claim,
            self.reader,
            Reservation.Status.AVAILABLE,
            requested=local(2026, 3, 18),
            available=local(2026, 3, 19),
            deadline=now + timedelta(hours=5),
        )
        broken = self.entry(title="Broken", registered=False)
        EncryptedContent.objects.create(
            acquisition=broken.acquisitions.first(),
            lcp_content_id="failed",
            encrypted_path="x",
            status=EncryptedContent.EncryptionStatus.FAILED,
        )
        stuck = self.entry(title="Stuck", registered=False)
        EncryptedContent.objects.create(
            acquisition=stuck.acquisitions.first(),
            lcp_content_id="stuck",
            encrypted_path="x",
            status=EncryptedContent.EncryptionStatus.PENDING,
            last_dispatched_at=now - timedelta(hours=2),
        )

        stats = self.build(now=now)
        attention = stats.attention
        self.assertEqual(attention.encryption_failed, 1)
        self.assertEqual(attention.encryption_stuck, 1)
        self.assertEqual(attention.lcp_not_lendable, 2)
        self.assertEqual(attention.claims_expiring_24h, 1)
        # Busy: its one copy is lent and someone waits. Claim: the copy is held for the claimant, nobody queues.
        self.assertEqual(attention.saturated_with_queue, 1)
        self.assertEqual(stats.kpis.copies_in_use_now, 2)


class StatisticsBucketTests(StatisticsTestCase):
    def test_timezone_edge_lands_on_next_local_day(self):
        book = self.entry()
        created = datetime(2026, 3, 9, 23, 30, tzinfo=dt_timezone.utc)  # 00:30 on 10 March in Bratislava
        self.license(book, self.reader, License.LicenseState.EXPIRED, created, created + timedelta(days=7))
        stats = self.build()
        started = {b.t: b.loans_started for b in stats.series if b.loans_started}
        self.assertEqual(started, {"2026-03-10": 1})
        (cell,) = [c for c in stats.heatmap if c.loans]
        self.assertEqual((cell.dow, cell.hour), (1, 0))  # Tuesday, midnight hour

    def test_partial_marks_only_cut_buckets(self):
        # 2026-03-02 and 2026-03-30 are Mondays.
        stats = self.build(date_from=date(2026, 3, 4), date_to=date(2026, 3, 30), bucket="week")
        self.assertEqual(
            [(b.t, b.partial) for b in stats.series],
            [
                ("2026-03-02", True),
                ("2026-03-09", False),
                ("2026-03-16", False),
                ("2026-03-23", False),
                ("2026-03-30", True),
            ],
        )
        monthly = self.build(date_from=date(2026, 1, 1), date_to=date(2026, 3, 15), bucket="month")
        self.assertEqual(
            [(b.t, b.partial) for b in monthly.series],
            [("2026-01-01", False), ("2026-02-01", False), ("2026-03-01", True)],
        )

    def test_series_is_zero_filled(self):
        stats = self.build()
        self.assertEqual(len(stats.series), 30)
        self.assertTrue(all(b.loans_started == 0 for b in stats.series))

    def test_empty_catalog_has_null_ratios(self):
        stats = self.build()
        self.assertEqual(len(stats.heatmap), 168)
        self.assertIsNone(stats.kpis.utilization.value)
        self.assertIsNone(stats.kpis.utilization.previous)
        self.assertIsNone(stats.kpis.hold_fulfillment.value)
        self.assertIsNone(stats.kpis.median_wait_hours.value)
        self.assertEqual(stats.kpis.loans.value, 0)
        self.assertEqual(stats.titles, [])
        self.assertFalse(stats.titles_truncated)

    def test_titles_truncated_at_limit(self):
        config = {"readium_enabled": True, "readium_amount": 1}
        Entry.objects.bulk_create(
            Entry(creator=self.admin, catalog=self.catalog, title=f"T{i}", config=config) for i in range(TITLES_LIMIT)
        )
        self.assertFalse(self.build().titles_truncated)
        Entry.objects.create(creator=self.admin, catalog=self.catalog, title="One more", config=config)
        stats = self.build()
        self.assertTrue(stats.titles_truncated)
        self.assertEqual(len(stats.titles), TITLES_LIMIT)
        self.assertEqual(stats.kpis.lcp_titles, TITLES_LIMIT + 1)

    def test_categories_top_by_loans(self):
        book = self.entry()
        category = Category.objects.create(
            creator=self.admin, catalog=self.catalog, term="004", label={"sk": "Informatika"}
        )
        book.categories.add(category)
        self.license(book, self.reader, License.LicenseState.EXPIRED, local(2026, 3, 2), local(2026, 3, 9))
        (row,) = self.build().categories
        self.assertEqual((row.title, row.loans), ("Informatika", 1))


class StatisticsEndpointTests(StatisticsTestCase):
    URL = "/api/v1/statistics"

    def get(self, user, **params):
        query = {"catalog_id": str(self.catalog.pk), "from": "2026-03-01", "to": "2026-03-30", "bucket": "week"}
        query.update(params)
        with patch.object(SecuredView, "_authenticate", lambda view, request: user):
            return self.client.get(self.URL, query)

    def test_permission_matrix(self):
        self.assertEqual(self.get(self.admin).status_code, 200)
        self.assertEqual(self.get(self.manager).status_code, 200)
        self.assertEqual(self.get(self.reader).status_code, 403)
        self.assertEqual(self.get(self.other_manager).status_code, 403)

    def test_unknown_catalog(self):
        self.assertEqual(self.get(self.admin, catalog_id="00000000-0000-0000-0000-000000000000").status_code, 404)

    def test_response_shape(self):
        body = self.get(self.manager).json()["response"]
        self.assertEqual(body["period"]["from"], "2026-03-01")
        self.assertEqual(body["period"]["bucket"], "week")
        self.assertEqual(len(body["heatmap"]), 168)

    def test_validation(self):
        for params in (
            {"from": "2026-03-30", "to": "2026-03-01"},
            {"from": "2025-01-01", "to": "2026-03-01"},
            {"from": "nope"},
            {"bucket": "year"},
            {"tz": "Mars/Olympus"},
            {"catalog_id": "x"},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.get(self.admin, **params).status_code, 400)

    def test_cached_for_everyone_after_access_check(self):
        self.assertEqual(self.get(self.manager).status_code, 200)
        self.assertEqual(self.get(self.reader).status_code, 403)
