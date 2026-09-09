import os
import sys
import unittest
from unittest.mock import Mock

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.annotation_client import AnnotationClient
from dp_python_lib.client.annotations_client import AnnotationsClient
from dp_python_lib.client.dataset_client import DataSetClient
from dp_python_lib.client.export_client import ExportClient
from dp_python_lib.client.machine_config_client import MachineConfigClient
from dp_python_lib.client.pv_metadata_client import PvMetadataClient
from dp_python_lib.client.sample_status_client import SampleStatusClient


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


if __name__ == "__main__":
    unittest.main()
