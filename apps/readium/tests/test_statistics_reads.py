"""#194: `reads` and `visitors` per bucket in `GET /api/v1/statistics`."""

import shutil
import tempfile
import time
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.core.auth import JWTFactory
from apps.core.models import (
    Acquisition,
    ApiKey,
    AuthSource,
    Catalog,
    Entry,
    ReadEvent,
    User,
    UserAcquisition,
    UserActivity,
)
from apps.core.services.reads import ReadService
from apps.core.views import SecuredView
from apps.readium.services import umami
from apps.readium.tests.test_statistics import LOCMEM_CACHE, NOW, TZ, StatisticsTestCase, local

MOBI = b"BOOKMOBI" + bytes(range(256)) * 8
UMAMI = {
    "EVILFLOWERS_UMAMI_URL": "http://umami.test",
    "EVILFLOWERS_UMAMI_WEBSITE_ID": "site",
    "EVILFLOWERS_UMAMI_API_KEY": "key",
}


def read_at(entry, moment, via=ReadEvent.Via.ACQUISITION) -> ReadEvent:
    return ReadEvent.objects.create(
        entry=entry, catalog_id=entry.catalog_id, occurred_at=moment, via=via, authenticated=False
    )


class ReadsSeriesTests(StatisticsTestCase):
    def setUp(self):
        super().setUp()
        self.book = self.entry(title="Open book")

    def test_zero_filled_and_local_day_boundary(self):
        # 23:30 UTC on 9 March is 00:30 on 10 March in Bratislava.
        read_at(self.book, datetime(2026, 3, 9, 23, 30, tzinfo=dt_timezone.utc))
        read_at(self.book, local(2026, 3, 10, 12))
        read_at(self.book, local(2026, 3, 30, 23, 59))
        # Outside the period and in another catalog: not counted.
        read_at(self.book, local(2026, 3, 31, 0, 1))
        other = self.entry(title="Elsewhere", catalog=self.other_catalog)
        read_at(other, local(2026, 3, 10, 12))

        stats = self.build()
        self.assertEqual(len(stats.series), 30)
        reads = {b.t: b.reads for b in stats.series}
        self.assertTrue(all(value is not None for value in reads.values()))
        self.assertEqual({t: n for t, n in reads.items() if n}, {"2026-03-10": 2, "2026-03-30": 1})
        self.assertEqual(reads["2026-03-09"], 0)

    def test_week_and_month_buckets(self):
        read_at(self.book, local(2026, 3, 4, 9))  # Wednesday of the week of 2 March, which starts before `from`
        read_at(self.book, local(2026, 3, 8, 23, 30))  # Sunday night, same ISO week
        read_at(self.book, local(2026, 3, 9, 0, 30))  # Monday: next week
        weekly = self.build(date_from=date(2026, 3, 4), date_to=date(2026, 3, 30), bucket="week")
        self.assertEqual([b.reads for b in weekly.series], [2, 1, 0, 0, 0])
        monthly = self.build(date_from=date(2026, 2, 1), date_to=date(2026, 3, 31), bucket="month")
        self.assertEqual([b.reads for b in monthly.series], [0, 3])

    @override_settings(EVILFLOWERS_READ_EVENT_RETENTION_DAYS=400)
    def test_buckets_past_retention_are_null_not_zero(self):
        now = local(2026, 3, 20, 12)
        stats = self.build(date_from=date(2025, 2, 1), date_to=date(2026, 3, 15), bucket="month", now=now)
        self.assertIsNone(stats.series[0].reads)
        self.assertEqual(stats.series[-1].reads, 0)

    def test_visitors_left_to_the_view(self):
        self.assertTrue(all(b.visitors is None for b in self.build().series))


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None, CACHES=LOCMEM_CACHE, EVILFLOWERS_READ_DEDUP_SECONDS=1800)
class ReadEventWriteTests(TestCase):
    def setUp(self):
        cache.clear()
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)

        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        self.user = User(username="reader", name="Re", surname="Ader", auth_source=source, is_superuser=True)
        self.user.set_unusable_password()
        self.user.save()
        self.catalog = Catalog.objects.create(creator=self.user, title="Open", url_name="open", is_public=True)
        self.entry = Entry.objects.create(creator=self.user, catalog=self.catalog, title="Open Book")
        self.acquisition = Acquisition.objects.create(
            entry=self.entry, mime=Acquisition.AcquisitionMIME.MOBI, relation=Acquisition.AcquisitionType.OPEN_ACCESS
        )
        self.acquisition.content.save("book.bin", ContentFile(MOBI), save=True)
        self.acquisition_url = reverse("files:acquisition-download", kwargs={"acquisition_id": self.acquisition.pk})
        # `check_entry_read` refuses anonymous users on `/data/v1/acquisitions/{id}`, so today an anonymous
        # reader gets a file through a shared link: a SHARED user acquisition, readable by anyone.
        shared = UserAcquisition.objects.create(
            acquisition=self.acquisition, user=self.user, type=UserAcquisition.UserAcquisitionType.SHARED
        )
        self.url = reverse("files:user-acquisition-download", kwargs={"user_acquisition_id": shared.pk})

    def _auth(self) -> dict:
        key = ApiKey.objects.create(user=self.user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def test_anonymous_download_is_a_read_but_not_activity(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        (event,) = ReadEvent.objects.all()
        self.assertEqual(event.entry_id, self.entry.pk)
        self.assertEqual(event.catalog_id, self.catalog.pk)
        self.assertEqual(event.via, ReadEvent.Via.USER_ACQUISITION)
        self.assertFalse(event.authenticated)
        self.assertFalse(UserActivity.objects.exists())

    def test_anonymous_acquisition_endpoint_is_recorded_when_served(self):
        # Not reachable over HTTP today (see setUp); the write point is there for when it is.
        request = RequestFactory().get(self.acquisition_url)
        request.user = AnonymousUser()
        ReadService.record(
            self.entry, ReadEvent.Via.ACQUISITION, scope=f"acquisition:{self.acquisition.pk}", request=request
        )
        self.assertEqual(ReadEvent.objects.get().authenticated, False)
        self.assertFalse(UserActivity.objects.exists())

    def test_ranges_and_reloads_within_the_window_count_once(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        # pdf.js continuation ranges, then a viewer that starts again from byte 0, then a reload.
        self.assertEqual(self.client.get(self.url, headers={"Range": "bytes=1024-2047"}).status_code, 206)
        self.assertEqual(self.client.get(self.url, headers={"Range": "bytes=0-1023"}).status_code, 206)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(ReadEvent.objects.count(), 1)

        # Another browser behind the same address is another reader.
        self.client.get(self.url, headers={"User-Agent": "Other browser"})
        self.assertEqual(ReadEvent.objects.count(), 2)

    def test_after_the_window_it_is_a_new_read(self):
        self.client.get(self.url)
        cache.clear()  # the window's key expired
        self.client.get(self.url)
        self.assertEqual(ReadEvent.objects.count(), 2)

    def test_signed_in_open_counts_once_on_the_served_path(self):
        # A signed-in reader is redirected (303) to a new user acquisition per open; only the served copy counts.
        for _ in range(2):
            response = self.client.get(self.acquisition_url, headers=self._auth(), follow=True)
            self.assertEqual(response.status_code, 200, response.redirect_chain)
        (event,) = ReadEvent.objects.all()
        self.assertEqual(event.via, ReadEvent.Via.USER_ACQUISITION)
        self.assertTrue(event.authenticated)

    def test_lcp_reads_dedup_per_holder_and_title(self):
        ReadService.record(self.entry, ReadEvent.Via.LCP_LICENSE, scope=f"lcp:{self.entry.pk}", user=self.user)
        ReadService.record(self.entry, ReadEvent.Via.LCP_LICENSE, scope=f"lcp:{self.entry.pk}", user=self.user)
        self.assertEqual(ReadEvent.objects.filter(via=ReadEvent.Via.LCP_LICENSE, authenticated=True).count(), 1)

    def test_a_failing_insert_never_breaks_the_download(self):
        with patch.object(ReadEvent.objects, "create", side_effect=RuntimeError("db down")):
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_no_personal_data_in_the_table(self):
        names = {f.name for f in ReadEvent._meta.get_fields()}
        self.assertEqual(names, {"id", "entry", "catalog_id", "occurred_at", "via", "authenticated"})
        request = RequestFactory().get("/", REMOTE_ADDR="10.1.2.3", HTTP_USER_AGENT="UA")
        request.user = AnonymousUser()
        ReadService.record(self.entry, ReadEvent.Via.ACQUISITION, scope="x", request=request)
        row = ReadEvent.objects.values().get()
        self.assertNotIn("10.1.2.3", repr(row))


class VisitorsEndpointTests(StatisticsTestCase):
    URL = "/api/v1/statistics"

    def get(self, **params):
        query = {
            "catalog_id": str(self.catalog.pk),
            "from": "2026-03-01",
            "to": "2026-03-07",
            "bucket": "day",
            "tz": TZ,
        }
        query.update(params)
        with patch.object(SecuredView, "_authenticate", lambda view, request: self.manager):
            return self.client.get(self.URL, query)

    def test_not_configured_is_null_without_a_call(self):
        with patch.object(requests, "get") as get:
            body = self.get().json()["response"]
        get.assert_not_called()
        self.assertTrue(all(b["visitors"] is None for b in body["series"]))
        self.assertTrue(all(b["reads"] == 0 for b in body["series"]))

    @override_settings(**UMAMI)
    def test_umami_outage_still_serves_everything_else(self):
        book = self.entry()
        read_at(book, local(2026, 3, 2, 10))
        with patch.object(requests, "get", side_effect=requests.ConnectionError("refused")):
            response = self.get()
        self.assertEqual(response.status_code, 200)
        body = response.json()["response"]
        self.assertEqual(len(body["series"]), 7)
        self.assertTrue(all(b["visitors"] is None for b in body["series"]))
        self.assertEqual([b["reads"] for b in body["series"]], [0, 1, 0, 0, 0, 0, 0])
        self.assertEqual(len(body["heatmap"]), 168)

    @override_settings(**UMAMI, EVILFLOWERS_UMAMI_TIMEOUT=0.3)
    def test_slow_umami_is_cut_at_the_budget(self):
        def slow(*args, **kwargs):
            time.sleep(1.5)
            raise requests.Timeout()

        started = time.monotonic()
        with patch.object(requests, "get", side_effect=slow):
            response = self.get()
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(b["visitors"] is None for b in response.json()["response"]["series"]))

    @override_settings(**UMAMI)
    def test_visitors_per_bucket_and_both_response_shapes(self):
        calls = []

        def answer(url, params, headers, timeout):
            calls.append(params)
            reply = MagicMock(status_code=200)
            # Early Umami 2.x wraps values as {value, prev}; later releases return plain numbers.
            day = datetime.fromtimestamp(params["startAt"] / 1000, tz=dt_timezone.utc).day
            reply.json.return_value = {"visitors": {"value": 3, "prev": 1}} if day % 2 else {"visitors": 5}
            return reply

        with patch.object(requests, "get", side_effect=answer):
            body = self.get().json()["response"]
        self.assertEqual(len(calls), 7)
        # Half-open buckets in the request's zone: 1 March 00:00 Bratislava is 28 Feb 23:00 UTC.
        first = min(calls, key=lambda p: p["startAt"])
        self.assertEqual(first["startAt"], int(local(2026, 3, 1).timestamp() * 1000))
        self.assertEqual(first["endAt"], int(local(2026, 3, 2).timestamp() * 1000) - 1)
        self.assertTrue(all(b["visitors"] in (3, 5) for b in body["series"]))

    @override_settings(**UMAMI)
    def test_past_buckets_are_cached_and_null_is_not(self):
        windows = [(local(2026, 3, 1), local(2026, 3, 2)), (local(2026, 3, 2), local(2026, 3, 3))]
        reply = MagicMock(status_code=200)
        reply.json.return_value = {"visitors": 7}
        with patch.object(requests, "get", side_effect=[reply, requests.ConnectionError()]) as get:
            first = umami.visitors_per_window(windows, NOW)
        self.assertEqual(sorted(first, key=str), sorted([7, None], key=str))
        with patch.object(requests, "get", return_value=reply) as get:
            self.assertEqual(umami.visitors_per_window(windows, NOW), [7, 7])
        self.assertEqual(get.call_count, 1)

    def test_no_user_ids_in_the_response(self):
        book = self.entry()
        read_at(book, local(2026, 3, 2, 10))
        text = self.get().content.decode()
        for user in (self.admin, self.manager, self.reader):
            self.assertNotIn(str(user.pk), text)

    def test_reads_count_matches_rows_for_one_day(self):
        book = self.entry()
        for hour in (0, 7, 23):
            read_at(book, local(2026, 3, 3, hour, 10))
        body = self.get().json()["response"]
        day = next(b for b in body["series"] if b["t"] == "2026-03-03")
        start, end = local(2026, 3, 3), local(2026, 3, 4)
        self.assertEqual(day["reads"], ReadEvent.objects.filter(occurred_at__gte=start, occurred_at__lt=end).count())
