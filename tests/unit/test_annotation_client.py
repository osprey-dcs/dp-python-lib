import os
import sys
import unittest
from unittest.mock import Mock

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.annotation_client import AnnotationClient
from dp_python_lib.client.annotations_client import AnnotationsClient
from dp_python_lib.client.dataset_client import DataSetClient
from dp_python_lib.client.export_client import DEFAULT_EXPORT_TIMEOUT_SECONDS, ExportClient
from dp_python_lib.client.machine_config_client import MachineConfigClient
from dp_python_lib.client.pv_metadata_client import PvMetadataClient
from dp_python_lib.client.sample_status_client import SampleStatusClient
from dp_python_lib.client.service_api_client_base import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    ClientTimeout,
)


class TestAnnotationClientFacade(unittest.TestCase):
    """
    The facade groups every DpAnnotationService feature client under one object sharing one channel.  These tests
    pin the wiring: a missing or misnamed attribute is otherwise only caught at a call site.
    """

    def setUp(self):
        self.channel = Mock()
        self.client = AnnotationClient(self.channel)

    def test_exposes_every_feature_client(self):
        expected = {
            "pv_metadata": PvMetadataClient,
            "machine_config": MachineConfigClient,
            "sample_status": SampleStatusClient,
            "datasets": DataSetClient,
            "annotations": AnnotationsClient,
            "export": ExportClient,
        }
        for attribute, client_class in expected.items():
            with self.subTest(attribute=attribute):
                self.assertIsInstance(getattr(self.client, attribute), client_class)

    def test_all_feature_clients_share_the_one_channel(self):
        for attribute in ("pv_metadata", "machine_config", "sample_status", "datasets", "annotations", "export"):
            with self.subTest(attribute=attribute):
                self.assertIs(getattr(self.client, attribute)._channel, self.channel)

    def test_each_feature_client_has_its_own_stub(self):
        # ServiceApiClientBase creates the stub once at init; the facade's clients must not share one instance.
        stubs = [
            self.client.datasets._stub,
            self.client.annotations._stub,
            self.client.export._stub,
        ]
        self.assertEqual(len({id(stub) for stub in stubs}), len(stubs))


class TestAnnotationClientTimeouts(unittest.TestCase):
    """The facade forwards its timeouts, and their sources, to the feature clients that use them (#81 D4)."""

    UNARY = ("pv_metadata", "machine_config", "sample_status", "datasets", "annotations")

    def test_forwards_each_timeout_with_its_source(self):
        client = AnnotationClient(
            Mock(),
            call_timeout=12,
            stream_idle_timeout=34,
            export_timeout=56,
            call_timeout_source="set by C",
            stream_idle_timeout_source="set by S",
            export_timeout_source="set by E",
        )
        for attribute in self.UNARY:
            with self.subTest(attribute=attribute):
                self.assertEqual(getattr(client, attribute)._call_timeout, ClientTimeout(12.0, "set by C"))
        self.assertEqual(client.sample_status._stream_idle_timeout, ClientTimeout(34.0, "set by S"))
        self.assertEqual(client.export._export_timeout, ClientTimeout(56.0, "set by E"))

    def test_omitted_timeouts_are_the_library_defaults(self):
        client = AnnotationClient(Mock())
        for attribute in self.UNARY:
            with self.subTest(attribute=attribute):
                self.assertEqual(getattr(client, attribute)._call_timeout.seconds, DEFAULT_CALL_TIMEOUT_SECONDS)
        self.assertEqual(client.sample_status._stream_idle_timeout.seconds, DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
        self.assertEqual(client.export._export_timeout.seconds, DEFAULT_EXPORT_TIMEOUT_SECONDS)

    def test_zero_disables(self):
        client = AnnotationClient(Mock(), call_timeout=0, stream_idle_timeout=0, export_timeout=0)
        self.assertIsNone(client.datasets._call_timeout.seconds)
        self.assertIsNone(client.sample_status._stream_idle_timeout.seconds)
        self.assertIsNone(client.export._export_timeout.seconds)


if __name__ == "__main__":
    unittest.main()
