import logging
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import data_frame_conversions as dfc
from dp_python_lib.client.annotations_client import (
    AnnotationQuery,
    SaveAnnotationRequestParams,
    calculations,
)
from dp_python_lib.client.dataset_client import (
    DataSetQuery,
    SaveDataSetRequestParams,
    data_block,
)
from dp_python_lib.client.export_client import ExportDataRequestParams, ExportFormat, calculations_spec
from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.grpc import common_pb2, ingestion_pb2, ingestion_pb2_grpc


class TestDataSetsAnnotationsIntegration(unittest.TestCase):
    """
    Integration tests for DataSetClient and AnnotationsClient that require a running MLDP ecosystem.

    Prerequisites:
    - MLDP services running (annotation service at localhost:50053 AND ingestion service at localhost:50051),
      however started
    - The Annotation Service must carry the modernized DataSet/Annotation API: dp-grpc 1.16.0 or later, built from
      dp-service main at or after PR #264

    To run these tests:
    1. Start the MLDP ecosystem
    2. Run: python -m unittest tests.integration.test_datasets_annotations_integration -v

    Why ingestion is a prerequisite here: saveDataSet validates that every PV named in a data block already exists
    IN THE ARCHIVE.  Despite its error text ("no PV metadata found for names: ..."), the server's check is a
    distinct on pvName over the buckets collection (MongoAnnotationHandler.validateSaveDataSetRequest ->
    MongoSyncQueryClient.executeQueryPvExistence), so saving PV metadata is not enough -- the PV must have ingested
    data.  This class therefore ingests a few samples for its run-unique PV in setUpClass, using the generated
    ingestion stub directly: the library's IngestionClient wraps only registerProvider() today, and ingestData() is
    issue #17.

    Each test writes under a run-unique owner id and tag and deletes what it wrote, so runs neither collide with
    each other nor with real data.
    """

    ANNOTATION_ADDRESS = "localhost:50053"
    INGESTION_ADDRESS = "localhost:50051"

    # Samples ingested for the run's PV, so a data block over them has something to reference.
    SAMPLE_COUNT = 5
    SAMPLE_PERIOD_NANOS = 1_000_000_000

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cls.logger = logging.getLogger(__name__)
        cls.logger.info("Setting up datasets/annotations integration test environment")

        cls._verify_services_available()

        cls.client = MldpClient()
        cls.datasets = cls.client.annotation.datasets
        cls.annotations = cls.client.annotation.annotations
        cls.export = cls.client.annotation.export

        cls._verify_modernized_api_available()

        # Run-unique namespace, so concurrent or repeated runs cannot see each other's records.
        cls.run_id = str(int(time.time() * 1000))
        cls.owner_id = f"itest_owner_{cls.run_id}"
        cls.tag = f"itest-tag-{cls.run_id}"
        cls.pv_name = f"ITEST:DATASET:{cls.run_id}"

        # A fixed, whole-second base time well clear of "now", so the range is stable across the run.
        cls.begin_time = datetime(2024, 2, 2, 18, 0, 0, tzinfo=timezone.utc)
        cls.end_time = cls.begin_time + timedelta(hours=1)

        cls.logger.info("Using owner=%s tag=%s pv=%s", cls.owner_id, cls.tag, cls.pv_name)

        cls._ingest_samples_for_run_pv()

    @classmethod
    def _verify_services_available(cls):
        cls.logger.info("Checking if MLDP annotation and ingestion services are available")
        for label, address in (("annotation", cls.ANNOTATION_ADDRESS), ("ingestion", cls.INGESTION_ADDRESS)):
            try:
                channel = grpc.insecure_channel(address)
                grpc.channel_ready_future(channel).result(timeout=5)
                cls.logger.info("%s service is reachable at %s", label.capitalize(), address)
                channel.close()
            except grpc.FutureTimeoutError:
                raise unittest.SkipTest(
                    f"MLDP {label} service not available at {address}. "
                    "Please start the MLDP ecosystem before running integration tests."
                ) from None
            except Exception as e:
                raise unittest.SkipTest(
                    f"Cannot connect to MLDP {label} service: {e}. Please ensure the MLDP ecosystem is running."
                ) from None

    @classmethod
    def _ingest_samples_for_run_pv(cls):
        """
        Ingests a handful of samples for this run's PV, so data blocks naming it pass saveDataSet's
        archive-existence check (see the class docstring).

        Uses the generated ingestion stub directly rather than the library, whose IngestionClient covers only
        registerProvider() today; wrapping ingestData() is issue #17.
        """
        channel = grpc.insecure_channel(cls.INGESTION_ADDRESS)
        cls._ingestion_channel = channel
        stub = ingestion_pb2_grpc.DpIngestionServiceStub(channel)

        registration = stub.registerProvider(
            ingestion_pb2.RegisterProviderRequest(providerName=f"itest_provider_{cls.run_id}"), timeout=10
        )
        if registration.HasField("exceptionalResult"):
            raise unittest.SkipTest(
                f"could not register an ingestion provider: {registration.exceptionalResult.message}"
            )
        provider_id = registration.registrationResult.providerId

        request = ingestion_pb2.IngestDataRequest(providerId=provider_id, clientRequestId=f"itest-{cls.run_id}")
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

        cls._await_pv_in_archive()
        cls.logger.info("Ingested %d samples for %s", cls.SAMPLE_COUNT, cls.pv_name)

    @classmethod
    def _await_pv_in_archive(cls, attempts=20, delay_seconds=0.5):
        """
        Waits until the ingested PV is visible to saveDataSet's existence check.

        ingestData() acks once the request is accepted, which is before the bucket is committed and queryable, so a
        saveDataSet issued immediately afterwards still fails the check.  Probe with a throwaway save rather than
        sleeping a fixed interval.
        """
        probe_params = SaveDataSetRequestParams(
            name=f"itest archive probe {cls.run_id}",
            owner_id=cls.owner_id,
            data_blocks=[data_block(cls.begin_time, cls.end_time, [cls.pv_name])],
        )
        for _ in range(attempts):
            result = cls.datasets.save_dataset(probe_params)
            if not result.result_status.is_error:
                cls.datasets.delete_dataset(result.dataset_id)
                return
            time.sleep(delay_seconds)

        raise unittest.SkipTest(
            f"ingested data for {cls.pv_name} did not become visible to saveDataSet within "
            f"{attempts * delay_seconds:.0f}s: {result.result_status.message}"
        )

    @classmethod
    def tearDownClass(cls):
        channel = getattr(cls, "_ingestion_channel", None)
        if channel is not None:
            channel.close()

    @classmethod
    def _verify_modernized_api_available(cls):
        """
        Skips if the reachable Annotation Service predates the modernized DataSet/Annotation API.

        Reachability alone is not enough: a pre-1.16.0 server accepts the connection and then answers getDataSet
        with UNIMPLEMENTED, which would surface as a wall of assertion failures rather than as the "backend not
        available" skip these tests intend.  Probe with a harmless get of a well-formed but absent ObjectId; a
        server that has the API answers with a "not found" business error rather than a missing-method gRPC error.
        """
        result = cls.datasets.get_dataset("000000000000000000000000")
        message = result.result_status.message or ""
        if result.result_status.is_error and "Method not found" in message:
            raise unittest.SkipTest(
                f"Annotation service at {cls.ANNOTATION_ADDRESS} does not implement the modernized DataSet API "
                f"({message}). It is new in dp-grpc 1.16.0; upgrade the server to run these tests."
            )
        cls.logger.info("Modernized DataSet/Annotation API is available")

    def setUp(self):
        # Ids written by the running test, torn down in reverse order (annotations before datasets, since
        # delete_dataset is refused while a dataset is referenced).
        self.created_dataset_ids = []
        self.created_annotation_ids = []

    def tearDown(self):
        for annotation_id in reversed(self.created_annotation_ids):
            self.annotations.delete_annotation(annotation_id)
        for dataset_id in reversed(self.created_dataset_ids):
            self.datasets.delete_dataset(dataset_id)

    def _save_dataset(self, name_suffix="", **overrides):
        """Saves a dataset in this run's namespace, registers it for teardown, and returns its id."""
        kwargs = {
            "name": f"itest dataset {self.run_id}{name_suffix}",
            "owner_id": self.owner_id,
            "data_blocks": [data_block(self.begin_time, self.end_time, [self.pv_name])],
            "tags": [self.tag],
            "attributes": {"runId": self.run_id},
            "modified_by": self.owner_id,
        }
        kwargs.update(overrides)

        result = self.datasets.save_dataset(SaveDataSetRequestParams(**kwargs))
        self.assertFalse(result.result_status.is_error, f"save_dataset failed: {result.result_status.message}")
        self.assertTrue(result.dataset_id)
        self.created_dataset_ids.append(result.dataset_id)
        return result.dataset_id

    def _save_annotation(self, dataset_ids, name_suffix="", **overrides):
        """Saves an annotation in this run's namespace, registers it for teardown, and returns the result."""
        kwargs = {
            "name": f"itest annotation {self.run_id}{name_suffix}",
            "owner_id": self.owner_id,
            "dataset_ids": dataset_ids,
            "tags": [self.tag],
            "modified_by": self.owner_id,
        }
        kwargs.update(overrides)

        result = self.annotations.save_annotation(SaveAnnotationRequestParams(**kwargs))
        self.assertFalse(result.result_status.is_error, f"save_annotation failed: {result.result_status.message}")
        self.assertTrue(result.annotation_id)
        self.created_annotation_ids.append(result.annotation_id)
        return result

    @staticmethod
    def _calculations_frame(values=(12.7, 12.8, 12.9)):
        """
        A hand-built single-column Calculations payload.

        Phase 1 has no data_frame builder yet (that is Phase 2), so this constructs the protos directly -- which is
        also the escape hatch the builder is meant to complement rather than replace.

        Note the SamplingClock start time must be a real time: the server rejects an axis whose startTime is epoch
        zero, so a fixture built from datetime(1970, 1, 1) would fail validation rather than the assertion.
        """
        frame = common_pb2.DataFrame()
        clock = frame.dataTimestamps.samplingClock
        clock.startTime.epochSeconds = int(datetime(2024, 2, 2, 18, 0, 0, tzinfo=timezone.utc).timestamp())
        clock.startTime.nanoseconds = 0
        clock.periodNanos = 1_000_000_000
        clock.count = len(values)

        column = frame.doubleColumns.add()
        column.name = "x_rms"
        column.values[:] = list(values)
        column.metadata.provenance.process = "1 Hz RMS"
        source = column.metadata.provenance.derivedFrom.add()
        source.pvName = "BPMS:GUNB:314:X"

        return calculations({"bpm-statistics": frame})

    # ------------------------------------------------------------------
    # DataSet round trip
    # ------------------------------------------------------------------

    def test_dataset_save_get_round_trip(self):
        """save -> get returns the same content, with server-set audit fields populated."""
        dataset_id = self._save_dataset()

        result = self.datasets.get_dataset(dataset_id)

        self.assertFalse(result.result_status.is_error, result.result_status.message)
        dataset = result.dataset
        self.assertEqual(dataset.id, dataset_id)
        self.assertEqual(dataset.ownerId, self.owner_id)
        self.assertEqual(dataset.name, f"itest dataset {self.run_id}")
        self.assertEqual(len(dataset.dataBlocks), 1)
        self.assertEqual(list(dataset.dataBlocks[0].pvNames), [self.pv_name])
        self.assertEqual(dataset.dataBlocks[0].beginTime.epochSeconds, int(self.begin_time.timestamp()))
        self.assertEqual(dataset.dataBlocks[0].endTime.epochSeconds, int(self.end_time.timestamp()))
        self.assertEqual({(a.name, a.value) for a in dataset.attributes}, {("runId", self.run_id)})
        # createdTime is server-set; updatedTime is unset on create.
        self.assertTrue(dataset.HasField("createdTime"))

    def test_dataset_tags_are_normalized_lowercase(self):
        """Tags are lowercased, deduplicated, and sorted on save, so they read back normalized."""
        mixed_case_tag = f"ITest-Mixed-{self.run_id}"
        dataset_id = self._save_dataset(tags=[mixed_case_tag, mixed_case_tag.upper(), self.tag])

        dataset = self.datasets.get_dataset(dataset_id).dataset

        self.assertIn(mixed_case_tag.lower(), list(dataset.tags))
        self.assertNotIn(mixed_case_tag, list(dataset.tags))
        # Deduplicated: the two case variants collapse to one entry.
        self.assertEqual(list(dataset.tags).count(mixed_case_tag.lower()), 1)

    def test_dataset_get_not_found_is_a_business_error(self):
        result = self.datasets.get_dataset("000000000000000000000000")

        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.dataset)

    def test_dataset_query_by_each_criterion(self):
        """Each DataSetQuery criterion finds the dataset this run saved."""
        dataset_id = self._save_dataset()

        criteria_by_name = {
            "ids": [DataSetQuery.ids([dataset_id])],
            "owners": [DataSetQuery.owners([self.owner_id])],
            "name": [DataSetQuery.name(prefix=[f"itest dataset {self.run_id}"])],
            "pv_names": [DataSetQuery.pv_names([self.pv_name])],
            "tags": [DataSetQuery.tags([self.tag])],
            "attributes": [DataSetQuery.attributes("runId", [self.run_id])],
            "attributes_key_only": [
                DataSetQuery.attributes("runId"),
                DataSetQuery.owners([self.owner_id]),
            ],
        }

        for label, criteria in criteria_by_name.items():
            with self.subTest(criterion=label):
                found = [d.id for d in self.datasets.iter_datasets(criteria)]
                self.assertIn(dataset_id, found, f"{label} did not find the saved dataset")

    def test_dataset_replace_in_full_clears_omitted_fields(self):
        """Saving with an id replaces in full: fields omitted from the second save are cleared."""
        dataset_id = self._save_dataset(description="original description")
        self.assertEqual(self.datasets.get_dataset(dataset_id).dataset.description, "original description")

        # Re-save the same id without a description.
        self._save_dataset(dataset_id=dataset_id)

        self.assertEqual(self.datasets.get_dataset(dataset_id).dataset.description, "")

    def test_dataset_paging(self):
        """iter_datasets() follows page tokens across a run-unique tag with limit=1."""
        ids = {self._save_dataset(name_suffix=f" #{i}") for i in range(3)}

        found = {d.id for d in self.datasets.iter_datasets([DataSetQuery.tags([self.tag])], limit=1)}

        self.assertTrue(ids.issubset(found), f"paging missed some datasets: expected {ids}, found {found}")

    def test_malformed_page_token_is_a_business_error(self):
        """Page tokens are opaque keyset tokens; a malformed one is rejected rather than silently restarted."""
        result = self.datasets.query_datasets([DataSetQuery.tags([self.tag])], page_token="not-a-real-token")

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.datasets, [])

    def test_get_datasets_batch_fetch(self):
        """get_datasets() resolves several ids in one query, and tolerates one that resolves to nothing."""
        ids = [self._save_dataset(name_suffix=f" #{i}") for i in range(2)]

        found = self.datasets.get_datasets([*ids, "000000000000000000000000"])

        self.assertEqual(set(found), set(ids))
        for dataset_id in ids:
            self.assertEqual(found[dataset_id].id, dataset_id)

    # ------------------------------------------------------------------
    # Annotation round trip
    # ------------------------------------------------------------------

    def test_annotation_save_get_round_trip_with_calculations(self):
        """save with calculations -> get returns them inline, keyed by the returned calculationsId."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._calculations_frame())

        self.assertTrue(saved.calculations_id, "saving with calculations must return a calculationsId")

        result = self.annotations.get_annotation(saved.annotation_id)

        self.assertFalse(result.result_status.is_error, result.result_status.message)
        annotation = result.annotation
        self.assertEqual(annotation.id, saved.annotation_id)
        self.assertEqual(list(annotation.dataSetIds), [dataset_id])
        self.assertEqual(annotation.calculationsId, saved.calculations_id)

        # getAnnotation is the only method that returns calculations inline.
        inline = result.calculations
        self.assertIsNotNone(inline)
        self.assertEqual([f.name for f in inline.calculationDataFrames], ["bpm-statistics"])
        frame = inline.calculationDataFrames[0].frame
        self.assertEqual([c.name for c in frame.doubleColumns], ["x_rms"])
        self.assertEqual(list(frame.doubleColumns[0].values), [12.7, 12.8, 12.9])
        self.assertEqual(frame.doubleColumns[0].metadata.provenance.process, "1 Hz RMS")
        self.assertEqual(frame.doubleColumns[0].metadata.provenance.derivedFrom[0].pvName, "BPMS:GUNB:314:X")

    def test_annotation_without_calculations_returns_empty_calculations_id(self):
        """No calculations in the request means an empty calculationsId -- empty string, not an error."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id])

        self.assertEqual(saved.calculations_id, "")
        self.assertIsNone(self.annotations.get_annotation(saved.annotation_id).calculations)

    def test_get_calculations_click_through(self):
        """queryAnnotations gives an id but no content; get_calculations() fetches the content it names."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._calculations_frame())

        # Query results carry the id but leave the calculations empty.
        from_query = [a for a in self.annotations.iter_annotations([AnnotationQuery.tags([self.tag])])]
        matching = [a for a in from_query if a.id == saved.annotation_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].calculationsId, saved.calculations_id)
        self.assertEqual(len(matching[0].calculations.calculationDataFrames), 0)

        # The click-through fetches it.
        result = self.annotations.get_calculations(saved.calculations_id)

        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertEqual([f.name for f in result.calculations.calculationDataFrames], ["bpm-statistics"])

    def test_annotation_replace_without_calculations_clears_them(self):
        """A full replace omitting calculations clears AND deletes the stored object."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._calculations_frame())
        original_calculations_id = saved.calculations_id

        # Re-save the same annotation id with no calculations.
        replaced = self._save_annotation([dataset_id], annotation_id=saved.annotation_id)

        self.assertEqual(replaced.annotation_id, saved.annotation_id)
        self.assertEqual(replaced.calculations_id, "")
        self.assertIsNone(self.annotations.get_annotation(saved.annotation_id).calculations)

        # The replaced calculations object is deleted, not orphaned.
        orphan = self.annotations.get_calculations(original_calculations_id)
        self.assertTrue(orphan.result_status.is_error)

    def test_annotation_query_by_each_criterion(self):
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id])

        criteria_by_name = {
            "ids": [AnnotationQuery.ids([saved.annotation_id])],
            "owners": [AnnotationQuery.owners([self.owner_id])],
            "datasets": [AnnotationQuery.datasets([dataset_id])],
            "name": [AnnotationQuery.name(prefix=[f"itest annotation {self.run_id}"])],
            "tags": [AnnotationQuery.tags([self.tag])],
        }

        for label, criteria in criteria_by_name.items():
            with self.subTest(criterion=label):
                found = [a.id for a in self.annotations.iter_annotations(criteria)]
                self.assertIn(saved.annotation_id, found, f"{label} did not find the saved annotation")

    def test_annotation_get_not_found_is_a_business_error(self):
        result = self.annotations.get_annotation("000000000000000000000000")

        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.annotation)

    # ------------------------------------------------------------------
    # Delete semantics
    # ------------------------------------------------------------------

    def test_delete_dataset_is_refused_while_referenced(self):
        """A dataset an annotation targets cannot be deleted; the two-step teardown is the honest shape."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id])

        refused = self.datasets.delete_dataset(dataset_id)

        self.assertTrue(refused.result_status.is_error, "deleting a referenced dataset must be refused")
        self.assertIsNone(refused.dataset_id)

        # Delete the annotation first, then the dataset succeeds.
        deleted_annotation = self.annotations.delete_annotation(saved.annotation_id)
        self.assertFalse(deleted_annotation.result_status.is_error, deleted_annotation.result_status.message)
        self.created_annotation_ids.remove(saved.annotation_id)

        deleted_dataset = self.datasets.delete_dataset(dataset_id)
        self.assertFalse(deleted_dataset.result_status.is_error, deleted_dataset.result_status.message)
        self.assertEqual(deleted_dataset.dataset_id, dataset_id)
        self.created_dataset_ids.remove(dataset_id)

    def test_deleting_twice_is_a_business_error(self):
        """
        Delete-not-found is a REJECT, not a silent success.

        This is the behavior the pre-implementation triage corrected in the plan: the first delete succeeds, and the
        second reports "no ... record found" rather than reporting success for a no-op.
        """
        dataset_id = self._save_dataset()

        first = self.datasets.delete_dataset(dataset_id)
        self.assertFalse(first.result_status.is_error, first.result_status.message)
        self.created_dataset_ids.remove(dataset_id)

        second = self.datasets.delete_dataset(dataset_id)
        self.assertTrue(second.result_status.is_error, "deleting an absent dataset must be an error")
        self.assertIsNone(second.dataset_id)

    def test_delete_annotation_cascades_to_its_calculations(self):
        """Deleting an annotation deletes the calculations it owns."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._calculations_frame())

        self.assertFalse(self.annotations.get_calculations(saved.calculations_id).result_status.is_error)

        deleted = self.annotations.delete_annotation(saved.annotation_id)
        self.assertFalse(deleted.result_status.is_error, deleted.result_status.message)
        self.created_annotation_ids.remove(saved.annotation_id)

        gone = self.annotations.get_calculations(saved.calculations_id)
        self.assertTrue(gone.result_status.is_error, "the annotation's calculations must be deleted with it")

    def test_delete_annotation_twice_is_a_business_error(self):
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id])

        first = self.annotations.delete_annotation(saved.annotation_id)
        self.assertFalse(first.result_status.is_error, first.result_status.message)
        self.created_annotation_ids.remove(saved.annotation_id)

        second = self.annotations.delete_annotation(saved.annotation_id)
        self.assertTrue(second.result_status.is_error)
        self.assertIsNone(second.annotation_id)

    # ------------------------------------------------------------------
    # Builder-made calculations, read back through the conversions (Phase 2)
    # ------------------------------------------------------------------

    CALC_START = datetime(2024, 2, 2, 18, 0, 0, tzinfo=timezone.utc)
    CALC_PERIOD_NANOS = 250_000_000
    CALC_VALUES = (12.7, 12.8, 12.9)

    def _builder_calculations(self):
        """
        A Calculations payload built entirely through data_frame.py, with column-level provenance.

        Deliberately uses a sub-second period, so the axis round trip exercises the nanosecond arithmetic rather
        than whole seconds a float could also represent.
        """
        axis = dfb.sampling_clock(self.CALC_START, self.CALC_PERIOD_NANOS, len(self.CALC_VALUES))
        column = dfb.double_column(
            "x_rms",
            list(self.CALC_VALUES),
            metadata=dfb.column_metadata(
                tags=["derived"],
                attributes={"unit": "mm"},
                provenance=dfb.provenance(
                    source="itest-rig",
                    process="1 Hz RMS",
                    derived_from=[dfb.pv_source(self.pv_name, (self.begin_time, self.end_time))],
                ),
            ),
        )
        return calculations({"orbit-rms": dfb.data_frame(axis, [column])})

    def test_builder_calculations_round_trip_is_nanosecond_exact(self):
        """
        Save calculations built by data_frame.py, read them back, and check the axis reproduces exactly.

        This is the leg that would fail silently if any part of the path routed timestamps through float seconds:
        a float64 cannot represent present-day epoch nanoseconds, so the expanded positions would drift.
        """
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._builder_calculations())

        fetched = self.annotations.get_calculations(saved.calculations_id)
        self.assertFalse(fetched.result_status.is_error, fetched.result_status.message)

        frames = fetched.calculations.calculationDataFrames
        self.assertEqual([f.name for f in frames], ["orbit-rms"])
        frame = frames[0].frame

        start_nanos = int(self.CALC_START.timestamp()) * 1_000_000_000
        expected = [start_nanos + i * self.CALC_PERIOD_NANOS for i in range(len(self.CALC_VALUES))]
        self.assertEqual(dfc.data_frame_timestamps(frame), expected)

        self.assertEqual(dfc.data_frame_columns(frame), {"x_rms": list(self.CALC_VALUES)})

    def test_builder_provenance_survives_the_round_trip(self):
        """Column-level provenance is the reason calculations are worth storing; it must come back intact."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._builder_calculations())

        frame = self.annotations.get_calculations(saved.calculations_id).calculations.calculationDataFrames[0].frame
        metadata = dfc.column_metadata_dict(frame.doubleColumns[0])

        self.assertEqual(metadata["tags"], ["derived"])
        self.assertEqual(metadata["attributes"], {"unit": "mm"})
        self.assertEqual(metadata["provenance"]["source"], "itest-rig")
        self.assertEqual(metadata["provenance"]["process"], "1 Hz RMS")
        self.assertEqual(metadata["provenance"]["derived_from"][0]["pv_name"], self.pv_name)

    # ------------------------------------------------------------------
    # Export (Phase 4)
    # ------------------------------------------------------------------

    def test_calculations_only_csv_export(self):
        """
        A calculations-only export needs no ingested data of its own, which makes it the cheapest end-to-end
        check that exportData() works.
        """
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._builder_calculations())

        result = self.export.export_data(
            ExportDataRequestParams(
                ExportFormat.CSV,
                calculations_spec=calculations_spec(saved.calculations_id),
            )
        )

        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertTrue(result.file_path, "a successful export must report a server-side file path")
        # file_url is empty unless the deployment publishes over HTTP; empty is normal, not a failure.
        self.assertIsNotNone(result.file_url)

    def test_export_accepts_a_bare_format_string(self):
        """ExportFormat coercion is part of the params contract, so exercise it against the real server too."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._builder_calculations())

        result = self.export.export_data(
            ExportDataRequestParams("csv", calculations_spec=calculations_spec(saved.calculations_id))
        )

        self.assertFalse(result.result_status.is_error, result.result_status.message)

    def test_export_with_a_column_filter(self):
        """calculations_spec() narrows the export to named columns of named frames."""
        dataset_id = self._save_dataset()
        saved = self._save_annotation([dataset_id], calculations=self._builder_calculations())

        result = self.export.export_data(
            ExportDataRequestParams(
                ExportFormat.CSV,
                calculations_spec=calculations_spec(saved.calculations_id, {"orbit-rms": ["x_rms"]}),
            )
        )

        self.assertFalse(result.result_status.is_error, result.result_status.message)

    def test_export_of_an_unknown_calculations_id_is_rejected(self):
        result = self.export.export_data(
            ExportDataRequestParams(ExportFormat.CSV, calculations_spec=calculations_spec("000000000000000000000000"))
        )

        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.file_path)

    def test_dataset_export_to_hdf5(self):
        """The dataset path exports the archived samples the run ingested."""
        dataset_id = self._save_dataset()

        result = self.export.export_data(ExportDataRequestParams(ExportFormat.HDF5, dataset_id=dataset_id))

        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertTrue(result.file_path)


if __name__ == "__main__":
    unittest.main()
