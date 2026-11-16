"""IP-034: the strict native-API calls of `DataverseClient`, against canned responses."""

import json
from unittest import mock

import requests
from django.test import SimpleTestCase

from apps.dataverse.exceptions import (
    DataverseAuthError,
    DataverseError,
    DataverseNotFoundError,
    DataverseStaleError,
    DataverseTransientError,
)
from apps.dataverse.services.client import DataverseClient
from apps.dataverse.tests import fixtures


def _response(status=200, body=None, text=None):
    response = requests.Response()
    response.status_code = status
    response._content = (text if text is not None else json.dumps(body or {"status": "OK", "data": {}})).encode()
    return response


class ClientTests(SimpleTestCase):
    def setUp(self):
        self.client_ = DataverseClient("http://dv:8080/", "token-1")
        patcher = mock.patch("apps.dataverse.services.client.requests.request")
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self):
        args, kwargs = self.request.call_args
        return args[0], args[1], kwargs

    def test_latest_published_version(self):
        self.request.return_value = _response(body=fixtures.load("version_book_published"))
        version = self.client_.get_version("42")
        method, url, kwargs = self._call()
        self.assertEqual((method, url), ("GET", "http://dv:8080/api/datasets/42/versions/:latest-published"))
        self.assertEqual(kwargs["headers"]["X-Dataverse-key"], "token-1")
        self.assertEqual(version["versionState"], "RELEASED")

    def test_dataset_by_pid(self):
        self.request.return_value = _response(body=fixtures.load("dataset_book_draft"))
        dataset = self.client_.get_dataset_by_pid("doi:10.5072/FK2/BOOK42")
        _method, url, kwargs = self._call()
        self.assertEqual(url, "http://dv:8080/api/datasets/:persistentId/")
        self.assertEqual(kwargs["params"], {"persistentId": "doi:10.5072/FK2/BOOK42"})
        self.assertEqual(dataset["datasetType"], "book")

    def test_versions_include_deaccessioned(self):
        self.request.return_value = _response(body={"status": "OK", "data": [{"versionState": "DEACCESSIONED"}]})
        versions = self.client_.list_versions("doi:x")
        _method, url, kwargs = self._call()
        self.assertEqual(url, "http://dv:8080/api/datasets/:persistentId/versions")
        self.assertEqual(kwargs["params"], {"persistentId": "doi:x", "includeDeaccessioned": "true"})
        self.assertEqual(versions, [{"versionState": "DEACCESSIONED"}])

    def test_search_sorted_by_date(self):
        self.request.return_value = _response(body={"status": "OK", "data": {"total_count": 0, "items": []}})
        self.client_.search_datasets(start=100, per_page=50, subtree="elvira")
        _method, url, kwargs = self._call()
        self.assertEqual(url, "http://dv:8080/api/search")
        self.assertEqual(
            kwargs["params"],
            {
                "q": "*",
                "type": "dataset",
                "sort": "date",
                "order": "desc",
                "start": 100,
                "per_page": 50,
                "subtree": "elvira",
            },
        )

    def test_edit_metadata_replaces_with_optimistic_lock(self):
        self.request.return_value = _response(body={"status": "OK", "data": {"id": 42}})
        fields = [{"typeName": "title", "value": "X"}]
        self.client_.edit_metadata("doi:x", fields, "2026-11-16T10:15:30Z")
        method, url, kwargs = self._call()
        self.assertEqual((method, url), ("PUT", "http://dv:8080/api/datasets/:persistentId/editMetadata"))
        self.assertEqual(kwargs["params"], {"persistentId": "doi:x", "replace": "true"})
        self.assertEqual(
            json.loads(kwargs["data"]), {"fields": fields, "sourceLastUpdateTime": "2026-11-16T10:15:30Z"}
        )
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")

    def test_stale_timestamp_raises_stale(self):
        for response in (
            _response(409, text='{"status":"ERROR"}'),
            _response(400, text='{"status":"ERROR","message":"Dataset has been modified since sourceLastUpdateTime"}'),
        ):
            self.request.return_value = response
            with self.assertRaises(DataverseStaleError):
                self.client_.edit_metadata("doi:x", [], "t")

    def test_other_400_is_not_stale(self):
        self.request.return_value = _response(400, text='{"status":"ERROR","message":"bad field"}')
        with self.assertRaises(DataverseError) as caught:
            self.client_.edit_metadata("doi:x", [], "t")
        self.assertNotIsInstance(caught.exception, DataverseStaleError)

    def test_publish_updatecurrent(self):
        self.request.return_value = _response(body={"status": "OK", "data": {}})
        self.client_.publish("doi:x")
        method, url, kwargs = self._call()
        self.assertEqual((method, url), ("POST", "http://dv:8080/api/datasets/:persistentId/actions/:publish"))
        self.assertEqual(kwargs["params"], {"persistentId": "doi:x", "type": "updatecurrent"})

    def test_create_dataset(self):
        self.request.return_value = _response(201, body={"status": "OK", "data": {"id": 7, "persistentId": "doi:y"}})
        created = self.client_.create_dataset("elvira", {"datasetType": "book", "datasetVersion": {}})
        method, url, kwargs = self._call()
        self.assertEqual((method, url), ("POST", "http://dv:8080/api/dataverses/elvira/datasets"))
        self.assertEqual(json.loads(kwargs["data"])["datasetType"], "book")
        self.assertEqual(created, {"id": 7, "persistentId": "doi:y"})

    def test_failures_raise_by_kind(self):
        cases = [
            (404, DataverseNotFoundError),
            (401, DataverseAuthError),
            (403, DataverseAuthError),
            (503, DataverseTransientError),
        ]
        for status, error in cases:
            self.request.return_value = _response(status, text="nope")
            with self.assertRaises(error):
                self.client_.get_version("42")

    def test_network_failure_is_transient(self):
        self.request.side_effect = requests.ConnectionError("down")
        with self.assertRaises(DataverseTransientError):
            self.client_.get_dataset("42")
