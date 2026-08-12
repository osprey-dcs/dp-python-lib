import logging
import os
import sys
import time
import unittest

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.client.pv_metadata_client import PvMetadataQuery, SavePvMetadataRequestParams


class TestPvMetadataClientIntegration(unittest.TestCase):
    """
    Integration tests for PvMetadataClient that require a running MLDP ecosystem.

    Prerequisites:
    - MLDP services running via docker compose
    - Default configuration (localhost:50053 for annotation service)
    - Services should be healthy and accepting connections

    To run these tests:
    1. Start MLDP ecosystem: docker compose up -d
    2. Run tests: python -m unittest tests.integration.test_pv_metadata_client_integration -v
    """

    ANNOTATION_ADDRESS = "localhost:50053"

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cls.logger = logging.getLogger(__name__)
        cls.logger.info("Setting up PV metadata integration test environment")

        cls._verify_services_available()

        cls.client = MldpClient()
        cls.logger.info("MldpClient initialized successfully")

    @classmethod
    def _verify_services_available(cls):
        cls.logger.info("Checking if MLDP annotation service is available")
        try:
            channel = grpc.insecure_channel(cls.ANNOTATION_ADDRESS)
            grpc.channel_ready_future(channel).result(timeout=5)
            cls.logger.info("Annotation service is reachable at %s", cls.ANNOTATION_ADDRESS)
            channel.close()
        except grpc.FutureTimeoutError:
            raise unittest.SkipTest(
                f"MLDP annotation service not available at {cls.ANNOTATION_ADDRESS}. "
                "Please start the MLDP ecosystem with 'docker compose up -d' before running integration tests."
            ) from None
        except Exception as e:
            raise unittest.SkipTest(
                f"Cannot connect to MLDP annotation service: {e}. Please ensure the MLDP ecosystem is running."
            ) from None

    def test_save_get_query_delete_round_trip(self):
        """
        Exercises the full PV metadata lifecycle against real services and asserts real success at each step:
        save -> get by name -> get by alias -> query -> iterate -> delete -> get confirms deletion.
        """
        self.assertIsNotNone(self.client.annotation, "annotation client should be initialized")
        pv_client = self.client.annotation.pv_metadata

        timestamp = int(time.time())
        pv_name = f"INTEGRATION:TEST:{timestamp}"
        alias = f"alias-{timestamp}"
        tags = ["integration", "test"]
        attributes = {"framework": "unittest", "timestamp": str(timestamp)}
        description = "Integration test PV metadata"

        # Ensure the record is cleaned up even if an assertion fails partway through.
        self.addCleanup(pv_client.delete_pv_metadata, pv_name)

        # --- save ---
        save_result = pv_client.save_pv_metadata(
            SavePvMetadataRequestParams(
                pv_name=pv_name,
                aliases=[alias],
                tags=tags,
                attributes=attributes,
                modified_by="dp-python-lib-integration-test",
                description=description,
            )
        )
        self.assertFalse(
            save_result.result_status.is_error,
            f"savePvMetadata failed: {save_result.result_status.message}",
        )
        self.assertEqual(save_result.pv_name, pv_name)
        self.logger.info("Saved PV metadata for: %s", save_result.pv_name)

        # --- get by name: all saved fields should round-trip ---
        get_result = pv_client.get_pv_metadata(pv_name)
        self.assertFalse(
            get_result.result_status.is_error,
            f"getPvMetadata (by name) failed: {get_result.result_status.message}",
        )
        metadata = get_result.pv_metadata
        self.assertIsNotNone(metadata, "getPvMetadata should return a PvMetadata record")
        self.assertEqual(metadata.pvName, pv_name)
        self.assertIn(alias, list(metadata.aliases))
        self.assertEqual(sorted(metadata.tags), sorted(tags))
        self.assertEqual({a.name: a.value for a in metadata.attributes}, attributes)
        self.assertEqual(metadata.description, description)
        self.logger.info("Retrieved and verified PV metadata for: %s", metadata.pvName)

        # --- get by alias: should resolve to the same PV ---
        get_by_alias = pv_client.get_pv_metadata(alias)
        self.assertFalse(
            get_by_alias.result_status.is_error,
            f"getPvMetadata (by alias) failed: {get_by_alias.result_status.message}",
        )
        self.assertIsNotNone(get_by_alias.pv_metadata)
        self.assertEqual(
            get_by_alias.pv_metadata.pvName,
            pv_name,
            "alias lookup should resolve to the canonical PV name",
        )
        self.logger.info("Alias %s resolved to PV: %s", alias, get_by_alias.pv_metadata.pvName)

        # --- query by exact name: should return exactly this PV ---
        query_result = pv_client.query_pv_metadata([PvMetadataQuery.pv_name(exact=[pv_name])])
        self.assertFalse(
            query_result.result_status.is_error,
            f"queryPvMetadata failed: {query_result.result_status.message}",
        )
        queried_names = [pv.pvName for pv in query_result.pv_metadata_list]
        self.assertIn(pv_name, queried_names, "query by exact name should return the saved PV")
        self.logger.info("Query returned %d record(s)", len(query_result.pv_metadata_list))

        # --- iterate by exact name: paging iterator should yield the PV ---
        iterated_names = [pv.pvName for pv in pv_client.iter_pv_metadata([PvMetadataQuery.pv_name(exact=[pv_name])])]
        self.assertIn(pv_name, iterated_names, "iter_pv_metadata should yield the saved PV")
        self.logger.info("Iterator yielded %d record(s)", len(iterated_names))

        # --- delete ---
        delete_result = pv_client.delete_pv_metadata(pv_name)
        self.assertFalse(
            delete_result.result_status.is_error,
            f"deletePvMetadata failed: {delete_result.result_status.message}",
        )
        self.assertEqual(delete_result.pv_name, pv_name)
        self.logger.info("Deleted PV metadata for: %s", delete_result.pv_name)

        # --- get after delete: should now report a business error (not found) ---
        get_after_delete = pv_client.get_pv_metadata(pv_name)
        self.assertTrue(
            get_after_delete.result_status.is_error,
            "getPvMetadata after delete should return an error",
        )
        self.assertIsNone(get_after_delete.pv_metadata)
        self.logger.info(
            "Confirmed deletion; get after delete returned: %s",
            get_after_delete.result_status.message,
        )

        self.logger.info("PV metadata round-trip integration test completed successfully")


if __name__ == "__main__":
    unittest.main(verbosity=2)
