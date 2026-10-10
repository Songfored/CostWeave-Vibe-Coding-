import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from costweave.experiment_lab import ExperimentLab, LabInputError
from costweave.adapters.http import HTTPResponse, UrllibHTTPTransport


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = ExperimentLab(Path(self.temp.name))
        self.data = {"endpoint": "https://example.com/v1", "api_key": "fake-test-key", "profile": "compatible"}

    def test_list_deduplicates_and_retains_claim_status(self):
        response = HTTPResponse(200, json.dumps({"data": [{"id": "beta"}, {"id": "alpha"}, {"id": "beta"}, {"id": "bad name"}, {"id": "fake-test-key"}]}).encode(), {}, 1)
        with patch.object(UrllibHTTPTransport, "get_models", return_value=response) as get:
            result = self.lab.discover_models(self.data)
        self.assertEqual(result["models"], ["alpha", "beta"])
        self.assertFalse(result["verified_identity"])
        self.assertEqual(get.call_args.kwargs["url"], "https://example.com/v1/models")
        self.assertFalse(self.lab.connections)

    def test_responses_address_and_empty(self):
        self.data.update(profile="responses", endpoint="https://example.com/v1/responses")
        with patch.object(UrllibHTTPTransport, "get_models", return_value=HTTPResponse(200, b'{"data":[]}', {}, 1)) as get:
            self.assertEqual(self.lab.discover_models(self.data)["models"], [])
        self.assertEqual(get.call_args.kwargs["url"], "https://example.com/v1/models")

    def test_failures_do_not_echo_response_or_key(self):
        for status, body in [(401, b'fake-test-key'), (302, b'fake-test-key'), (200, b'fake-test-key'), (200, b'{"data":null}')]:
            with self.subTest(status=status, body=body), patch.object(UrllibHTTPTransport, "get_models", return_value=HTTPResponse(status, body, {}, 1)):
                with self.assertRaises(LabInputError) as error:
                    self.lab.discover_models(self.data)
                self.assertNotIn("fake-test-key", str(error.exception))

    def test_private_address_and_missing_key_never_send(self):
        for data in [dict(self.data, endpoint="https://127.0.0.1/v1"), dict(self.data, api_key="")]:
            with patch.object(UrllibHTTPTransport, "get_models") as get:
                with self.assertRaises(ValueError):
                    self.lab.discover_models(data)
                get.assert_not_called()

    def test_get_shares_pinned_transport(self):
        with patch.object(UrllibHTTPTransport, "_post_remote_pinned", return_value="ok") as send:
            self.assertEqual(UrllibHTTPTransport().get_models(url="https://example.com/v1/models", headers={}), "ok")
        self.assertEqual(send.call_args.kwargs["method"], "GET")
        self.assertEqual(send.call_args.kwargs["timeout_seconds"], 15)


if __name__ == "__main__":
    unittest.main()
