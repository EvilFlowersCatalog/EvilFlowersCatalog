from django.test import SimpleTestCase, override_settings

from apps.assistant import evaluation


class EvaluationRequestTests(SimpleTestCase):
    """#121: the eval's request must pass ALLOWED_HOSTS, or every `get_entry` raises DisallowedHost."""

    @override_settings(
        EVILFLOWERS_BASE_URL="https://dev.evilflowers.example.org", ALLOWED_HOSTS=["dev.evilflowers.example.org"]
    )
    def test_uses_the_instance_host_and_scheme(self):
        request = evaluation.evaluation_request("post")
        self.assertEqual(request.get_host(), "dev.evilflowers.example.org")
        self.assertTrue(request.is_secure())
        self.assertEqual(request.build_absolute_uri("/x.png"), "https://dev.evilflowers.example.org/x.png")

    @override_settings(EVILFLOWERS_BASE_URL="", ALLOWED_HOSTS=["localhost"])
    def test_falls_back_to_localhost(self):
        request = evaluation.evaluation_request("get")
        self.assertEqual(request.get_host(), "localhost")
        self.assertFalse(request.is_secure())
