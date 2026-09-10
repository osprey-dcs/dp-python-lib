"""
Integration coverage for the issue #40 / #41 relaxations, against a live MLDP ecosystem.

Both changes rest on a *server* behavior that unit tests cannot reach -- they assert only the shape of the request
the client builds, not that the server honors it:

  - #40 (`plan/tickets/40/plan.md`): an `attributesCriterion` carrying a key and no values is a key-only existence
    search.  Server side that is `Filters.exists("attributes.<key>")`; the failure mode this pins is the plausible
    alternative, an `$in: []` that matches NOTHING.  A unit test asserting `values == []` cannot tell those apart.
  - #41 (`plan/tickets/41/plan.md`): an omitted criteria list matches ALL records rather than being rejected.  The
    failure mode is a business error, which again only a real server can produce.

So each test here saves a record whose attribute value would NOT match any value-based query, then asserts the
key-only form finds it anyway.  That is the assertion that distinguishes a working existence search from an `$in`
against an empty list.

Prerequisites:
- MLDP services running (annotation service at localhost:50053; TestV2SelectorRelaxations also needs ingestion at
  localhost:50051).  Tests self-skip when they are absent.
- Run with: pytest tests/integration/test_query_helper_relaxations_integration.py -v

Every record is namespaced with a per-run id and removed via addCleanup, so repeated runs do not accumulate state
and a mid-test failure still tears down.
"""

import logging
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.machine_config_client import (
    ConfigurationActivationQuery,
    ConfigurationQuery,
    SaveConfigurationActivationRequestParams,
    SaveConfigurationRequestParams,
)
from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.client.pv_metadata_client import PvMetadataQuery, SavePvMetadataRequestParams
from dp_python_lib.client.query_client import PvQuery, QueryParams
from dp_python_lib.grpc import ingestion_pb2, ingestion_pb2_grpc

ANNOTATION_ADDRESS = "localhost:50053"
INGESTION_ADDRESS = "localhost:50051"

# The value stored on every probe record.  Key-only searches must find these WITHOUT naming the value; the
# NON_MATCHING_VALUE below is what a value-based query is asked for instead, to prove the two differ.
STORED_VALUE = "alpha"
NON_MATCHING_VALUE = "not-the-stored-value"


def _require_service(logger, label, address):
    """Skips the calling test class unless `address` accepts a connection within 5s."""
    try:
        channel = grpc.insecure_channel(address)
        grpc.channel_ready_future(channel).result(timeout=5)
        channel.close()
        logger.info("%s service is reachable at %s", label, address)
    except grpc.FutureTimeoutError:
        raise unittest.SkipTest(
            f"MLDP {label} service not available at {address}.  Start the MLDP ecosystem before running "
            "integration tests."
        ) from None
    except Exception as e:
        raise unittest.SkipTest(f"Cannot connect to MLDP {label} service: {e}.") from None


class TestAnnotationServiceRelaxations(unittest.TestCase):
    """
    Covers #40 and #41 on the three annotation-service query families: PV metadata, configurations, and
    configuration activations.
    """

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cls.logger = logging.getLogger(__name__)
        _require_service(cls.logger, "annotation", ANNOTATION_ADDRESS)

        cls.client = MldpClient()
        cls.run_id = int(time.time())
        cls.logger.info("Query-helper relaxation integration run id: %s", cls.run_id)

    # ------------------------------------------------------------------
    # PV metadata
    # ------------------------------------------------------------------

    def test_pv_metadata_key_only_and_browse_all(self):
        pv_client = self.client.annotation.pv_metadata
        pv_name = f"ITEST:RELAX:PV:{self.run_id}"
        # Per-test unique key, so a concurrent run's records cannot satisfy this test's assertions for it.
        key = f"itest_pv_key_{self.run_id}"

        self.addCleanup(pv_client.delete_pv_metadata, pv_name)
        save = pv_client.save_pv_metadata(
            SavePvMetadataRequestParams(
                pv_name=pv_name,
                attributes={key: STORED_VALUE},
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(save.result_status.is_error, f"savePvMetadata failed: {save.result_status.message}")

        # --- #40: key-only existence search finds it ---
        for label, criterion in (
            ("values omitted", PvMetadataQuery.attributes(key)),
            ("values empty list", PvMetadataQuery.attributes(key, [])),
        ):
            with self.subTest(form=label):
                result = pv_client.query_pv_metadata([criterion])
                self.assertFalse(
                    result.result_status.is_error,
                    f"key-only queryPvMetadata ({label}) failed: {result.result_status.message}",
                )
                self.assertIn(
                    pv_name,
                    [record.pvName for record in result.pv_metadata_list],
                    f"key-only search ({label}) must match on key existence alone",
                )

        # The assertion that makes the one above meaningful: the same key with a value that does NOT match
        # returns nothing, so the key-only hit came from existence rather than from matching everything.
        negative = pv_client.query_pv_metadata([PvMetadataQuery.attributes(key, [NON_MATCHING_VALUE])])
        self.assertFalse(negative.result_status.is_error, negative.result_status.message)
        self.assertNotIn(
            pv_name,
            [record.pvName for record in negative.pv_metadata_list],
            "a value-based query for a value the record does not have must not match it",
        )

        # --- #41: omitted criteria is match-all, not a rejection ---
        for label, page in (
            ("omitted", pv_client.query_pv_metadata()),
            ("empty list", pv_client.query_pv_metadata([])),
        ):
            with self.subTest(form=label):
                self.assertFalse(
                    page.result_status.is_error,
                    f"browse-all queryPvMetadata ({label}) must not be rejected: {page.result_status.message}",
                )

        # iter_* with no criteria is the documented browse-all form and must reach this run's record.  Bound the
        # scan: the archive is unbounded in principle, and the point here is reachability, not exhaustion.
        found = False
        for count, record in enumerate(pv_client.iter_pv_metadata()):
            if record.pvName == pv_name:
                found = True
                break
            if count >= 5000:
                self.skipTest("catalogue too large to confirm browse-all reachability within the scan bound")
        self.assertTrue(found, "iter_pv_metadata() with no criteria should reach the saved record")
        self.logger.info("PV metadata: key-only search and browse-all both verified against the live server")

    # ------------------------------------------------------------------
    # Configurations
    # ------------------------------------------------------------------

    def test_configuration_key_only_and_browse_all(self):
        mc = self.client.annotation.machine_config
        config_name = f"itest-relax-cfg-{self.run_id}"
        key = f"itest_cfg_key_{self.run_id}"

        self.addCleanup(mc.delete_configuration, config_name)
        save = mc.save_configuration(
            SaveConfigurationRequestParams(
                configuration_name=config_name,
                category="integration-test",  # required by the server
                attributes={key: STORED_VALUE},
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(save.result_status.is_error, f"saveConfiguration failed: {save.result_status.message}")

        for label, criterion in (
            ("values omitted", ConfigurationQuery.attributes(key)),
            ("values empty list", ConfigurationQuery.attributes(key, [])),
        ):
            with self.subTest(form=label):
                result = mc.query_configurations([criterion])
                self.assertFalse(
                    result.result_status.is_error,
                    f"key-only queryConfigurations ({label}) failed: {result.result_status.message}",
                )
                self.assertIn(
                    config_name,
                    [c.configurationName for c in result.configurations],
                    f"key-only search ({label}) must match on key existence alone",
                )

        negative = mc.query_configurations([ConfigurationQuery.attributes(key, [NON_MATCHING_VALUE])])
        self.assertFalse(negative.result_status.is_error, negative.result_status.message)
        self.assertNotIn(
            config_name,
            [c.configurationName for c in negative.configurations],
            "a value-based query for a value the record does not have must not match it",
        )

        browse = mc.query_configurations()
        self.assertFalse(
            browse.result_status.is_error,
            f"browse-all queryConfigurations must not be rejected: {browse.result_status.message}",
        )
        self.assertIn(
            config_name,
            [c.configurationName for c in mc.iter_configurations()],
            "iter_configurations() with no criteria should reach the saved configuration",
        )
        self.logger.info("Configurations: key-only search and browse-all both verified against the live server")

    # ------------------------------------------------------------------
    # Configuration activations
    # ------------------------------------------------------------------

    def test_configuration_activation_key_only_and_browse_all(self):
        mc = self.client.annotation.machine_config
        config_name = f"itest-relax-act-cfg-{self.run_id}"
        activation_id = f"itest-relax-act-{self.run_id}"
        key = f"itest_act_key_{self.run_id}"

        # An activation requires its configuration to exist; tear down in reverse order.
        self.addCleanup(mc.delete_configuration, config_name)
        self.addCleanup(mc.delete_configuration_activation, client_activation_id=activation_id)

        save_config = mc.save_configuration(
            SaveConfigurationRequestParams(
                configuration_name=config_name,
                category="integration-test",
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            save_config.result_status.is_error,
            f"saveConfiguration failed: {save_config.result_status.message}",
        )

        save = mc.save_configuration_activation(
            SaveConfigurationActivationRequestParams(
                configuration_name=config_name,
                start_time=datetime(2026, 3, 1, tzinfo=timezone.utc),
                end_time=datetime(2026, 3, 2, tzinfo=timezone.utc),
                client_activation_id=activation_id,
                attributes={key: STORED_VALUE},
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            save.result_status.is_error,
            f"saveConfigurationActivation failed: {save.result_status.message}",
        )

        for label, criterion in (
            ("values omitted", ConfigurationActivationQuery.attributes(key)),
            ("values empty list", ConfigurationActivationQuery.attributes(key, [])),
        ):
            with self.subTest(form=label):
                result = mc.query_configuration_activations([criterion])
                self.assertFalse(
                    result.result_status.is_error,
                    f"key-only queryConfigurationActivations ({label}) failed: {result.result_status.message}",
                )
                self.assertIn(
                    activation_id,
                    [a.clientActivationId for a in result.configuration_activations],
                    f"key-only search ({label}) must match on key existence alone",
                )

        negative = mc.query_configuration_activations(
            [ConfigurationActivationQuery.attributes(key, [NON_MATCHING_VALUE])]
        )
        self.assertFalse(negative.result_status.is_error, negative.result_status.message)
        self.assertNotIn(
            activation_id,
            [a.clientActivationId for a in negative.configuration_activations],
            "a value-based query for a value the record does not have must not match it",
        )

        browse = mc.query_configuration_activations()
        self.assertFalse(
            browse.result_status.is_error,
            f"browse-all queryConfigurationActivations must not be rejected: {browse.result_status.message}",
        )
        self.assertIn(
            activation_id,
            [a.clientActivationId for a in mc.iter_configuration_activations()],
            "iter_configuration_activations() with no criteria should reach the saved activation",
        )
        self.logger.info("Activations: key-only search and browse-all both verified against the live server")


class TestV2SelectorRelaxations(unittest.TestCase):
    """
    Covers #40 on the v2 query service selectors (`PvQuery.attr`), which #41 deliberately does not touch.

    This is the one path where the client-side non-blank-key check is the ONLY one there is: `QueryV2Resolver`
    does not validate the key, so a blank one would reach Mongo as an existence test on "attributes." and match
    nothing silently (`plan/tickets/40/plan.md` T5).  Reaching the selector at all needs archived samples, so this
    class ingests its own -- through the generated stub, since IngestionClient wraps only registerProvider()
    until #17, the same approach test_datasets_annotations_integration.py takes.
    """

    SAMPLE_COUNT = 5
    SAMPLE_PERIOD_NANOS = 1_000_000_000

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cls.logger = logging.getLogger(__name__)
        for label, address in (("annotation", ANNOTATION_ADDRESS), ("ingestion", INGESTION_ADDRESS)):
            _require_service(cls.logger, label, address)

        cls.client = MldpClient()
        cls.run_id = int(time.time())
        cls.pv_name = f"ITEST:RELAX:V2:{cls.run_id}"
        cls.attribute_key = f"itest_v2_key_{cls.run_id}"

        # Whole seconds, so the SamplingClock start lands exactly on the ingested axis.
        cls.begin_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
        cls.end_time = cls.begin_time + timedelta(seconds=cls.SAMPLE_COUNT)

        cls._ingest_samples()
        cls._catalogue_pv()

    @classmethod
    def tearDownClass(cls):
        # Guarded: an early skip can leave these unset.
        if getattr(cls, "client", None) is not None:
            cls.client.annotation.pv_metadata.delete_pv_metadata(cls.pv_name)
        if getattr(cls, "_ingestion_channel", None) is not None:
            cls._ingestion_channel.close()

    @classmethod
    def _ingest_samples(cls):
        """Ingests a few samples for this run's PV, so the v2 selector has something to select."""
        cls._ingestion_channel = grpc.insecure_channel(INGESTION_ADDRESS)
        stub = ingestion_pb2_grpc.DpIngestionServiceStub(cls._ingestion_channel)

        registration = stub.registerProvider(
            ingestion_pb2.RegisterProviderRequest(providerName=f"itest_relax_provider_{cls.run_id}"), timeout=10
        )
        if registration.HasField("exceptionalResult"):
            raise unittest.SkipTest(
                f"could not register an ingestion provider: {registration.exceptionalResult.message}"
            )

        request = ingestion_pb2.IngestDataRequest(
            providerId=registration.registrationResult.providerId,
            clientRequestId=f"itest-relax-{cls.run_id}",
        )
        clock = request.ingestionDataFrame.dataTimestamps.samplingClock
        clock.startTime.epochSeconds = int(cls.begin_time.timestamp())
        clock.periodNanos = cls.SAMPLE_PERIOD_NANOS
        clock.count = cls.SAMPLE_COUNT

        column = request.ingestionDataFrame.dataColumns.add()
        column.name = cls.pv_name
        for i in range(cls.SAMPLE_COUNT):
            column.dataValues.add().doubleValue = float(i)

        response = stub.ingestData(request, timeout=15)
        if response.HasField("exceptionalResult"):
            raise unittest.SkipTest(f"could not ingest test data: {response.exceptionalResult.message}")
        cls.logger.info("Ingested %d samples for %s", cls.SAMPLE_COUNT, cls.pv_name)

    @classmethod
    def _catalogue_pv(cls):
        """Gives the ingested PV the attribute the key-only selector will search on."""
        result = cls.client.annotation.pv_metadata.save_pv_metadata(
            SavePvMetadataRequestParams(
                pv_name=cls.pv_name,
                attributes={cls.attribute_key: STORED_VALUE},
                modified_by="dp-python-lib-integration-test",
            )
        )
        if result.result_status.is_error:
            raise unittest.SkipTest(f"could not catalogue the test PV: {result.result_status.message}")

    def _query_columns(self, criterion, attempts=20, delay_seconds=0.5):
        """
        Runs a v2 query selecting on `criterion`, returning the column names it produced.

        ingestData() acks before the bucket is queryable, so poll rather than sleeping a fixed interval -- the same
        reason test_datasets_annotations_integration.py probes for archive visibility.
        """
        params = QueryParams(
            begin_time=self.begin_time,
            end_time=self.end_time,
            pv_selector=PvQuery.metadata([criterion]),
        )
        for _ in range(attempts):
            result = self.client.query.query_samples(params)
            self.assertFalse(
                result.result_status.is_error,
                f"querySamples failed: {result.result_status.message}",
            )
            names = [column.name for column in result.column_table.dataColumns]
            if names:
                return names
            time.sleep(delay_seconds)
        return []

    def test_v2_key_only_attribute_selector_returns_samples(self):
        # --- #40 on the v2 path: a key-only selector selects the PV and returns its samples ---
        for label, criterion in (
            ("values omitted", PvQuery.attr(self.attribute_key)),
            ("values empty list", PvQuery.attr(self.attribute_key, [])),
        ):
            with self.subTest(form=label):
                self.assertIn(
                    self.pv_name,
                    self._query_columns(criterion),
                    f"key-only v2 selector ({label}) should select the PV by attribute existence",
                )

        # Same distinguishing check as the annotation-service tests: a non-matching value selects nothing, so the
        # hits above came from key existence rather than from an unfiltered match.
        self.assertEqual(
            self._query_columns(PvQuery.attr(self.attribute_key, [NON_MATCHING_VALUE]), attempts=1),
            [],
            "a value-based v2 selector for a value the PV does not have must select nothing",
        )
        self.logger.info("v2 selector: key-only attribute search verified against the live server")


if __name__ == "__main__":
    unittest.main(verbosity=2)
