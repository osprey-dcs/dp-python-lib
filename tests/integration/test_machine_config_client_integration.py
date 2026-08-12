import logging
import os
import sys
import time
import unittest
from datetime import datetime, timezone

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


class TestMachineConfigClientIntegration(unittest.TestCase):
    """
    Integration tests for MachineConfigClient that require a running MLDP ecosystem.

    Prerequisites:
    - MLDP services running (default annotation service at localhost:50053)
    - Services should be healthy and accepting connections

    To run these tests:
    1. Start MLDP ecosystem (e.g. docker compose up -d)
    2. Run: python -m unittest tests.integration.test_machine_config_client_integration -v
    """

    ANNOTATION_ADDRESS = "localhost:50053"

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cls.logger = logging.getLogger(__name__)
        cls.logger.info("Setting up machine config integration test environment")

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
                "Please start the MLDP ecosystem before running integration tests."
            ) from None
        except Exception as e:
            raise unittest.SkipTest(
                f"Cannot connect to MLDP annotation service: {e}. Please ensure the MLDP ecosystem is running."
            ) from None

    def test_configuration_round_trip(self):
        """
        Exercises the full Configuration lifecycle against real services and asserts real success at each step:
        save -> get -> query -> iterate -> delete -> get confirms deletion.
        """
        self.assertIsNotNone(self.client.annotation, "annotation client should be initialized")
        mc = self.client.annotation.machine_config

        timestamp = int(time.time())
        config_name = f"INTEGRATION:CFG:{timestamp}"
        category = f"integration-{timestamp}"
        tags = ["integration", "test"]
        attributes = {"framework": "unittest", "timestamp": str(timestamp)}
        description = "Integration test configuration"

        self.addCleanup(mc.delete_configuration, config_name)

        # --- save ---
        save_result = mc.save_configuration(
            SaveConfigurationRequestParams(
                configuration_name=config_name,
                category=category,
                description=description,
                tags=tags,
                attributes=attributes,
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            save_result.result_status.is_error,
            f"saveConfiguration failed: {save_result.result_status.message}",
        )
        self.assertEqual(save_result.configuration_name, config_name)
        self.logger.info("Saved configuration: %s", save_result.configuration_name)

        # --- get: saved fields should round-trip ---
        get_result = mc.get_configuration(config_name)
        self.assertFalse(
            get_result.result_status.is_error,
            f"getConfiguration failed: {get_result.result_status.message}",
        )
        config = get_result.configuration
        self.assertIsNotNone(config, "getConfiguration should return a Configuration record")
        self.assertEqual(config.configurationName, config_name)
        self.assertEqual(config.category, category)
        self.assertEqual(sorted(config.tags), sorted(tags))
        self.assertEqual({a.name: a.value for a in config.attributes}, attributes)
        self.assertEqual(config.description, description)
        self.logger.info("Retrieved and verified configuration: %s", config.configurationName)

        # --- query by exact name ---
        query_result = mc.query_configurations([ConfigurationQuery.name(exact=[config_name])])
        self.assertFalse(
            query_result.result_status.is_error,
            f"queryConfigurations failed: {query_result.result_status.message}",
        )
        queried_names = [c.configurationName for c in query_result.configurations]
        self.assertIn(config_name, queried_names, "query by exact name should return the saved configuration")
        self.logger.info("Query returned %d record(s)", len(query_result.configurations))

        # --- iterate by category ---
        iterated_names = [
            c.configurationName for c in mc.iter_configurations([ConfigurationQuery.category([category])])
        ]
        self.assertIn(config_name, iterated_names, "iter_configurations should yield the saved configuration")
        self.logger.info("Iterator yielded %d record(s)", len(iterated_names))

        # --- delete ---
        delete_result = mc.delete_configuration(config_name)
        self.assertFalse(
            delete_result.result_status.is_error,
            f"deleteConfiguration failed: {delete_result.result_status.message}",
        )
        self.assertEqual(delete_result.configuration_name, config_name)
        self.logger.info("Deleted configuration: %s", delete_result.configuration_name)

        # --- get after delete should report a business error ---
        get_after_delete = mc.get_configuration(config_name)
        self.assertTrue(
            get_after_delete.result_status.is_error,
            "getConfiguration after delete should return an error",
        )
        self.assertIsNone(get_after_delete.configuration)
        self.logger.info(
            "Confirmed deletion; get after delete returned: %s",
            get_after_delete.result_status.message,
        )

    def test_configuration_activation_round_trip(self):
        """
        Exercises the full ConfigurationActivation lifecycle against real services:
        save a configuration, save an activation, get it both ways (by id and composite key), query, iterate,
        confirm getActiveConfigurations sees it during its interval, then delete both records.
        """
        self.assertIsNotNone(self.client.annotation, "annotation client should be initialized")
        mc = self.client.annotation.machine_config

        now = int(time.time())
        config_name = f"INTEGRATION:ACT-CFG:{now}"
        client_activation_id = f"integration-act-{now}"
        # An interval that comfortably brackets "now" so getActiveConfigurations should find it.
        start_time = now - 3600
        end_time = now + 3600

        self.addCleanup(mc.delete_configuration, config_name)
        self.addCleanup(mc.delete_configuration_activation, client_activation_id)

        # A configuration must exist before it can be activated.
        save_config = mc.save_configuration(
            SaveConfigurationRequestParams(
                configuration_name=config_name,
                category="integration-activation",
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            save_config.result_status.is_error,
            f"saveConfiguration (for activation) failed: {save_config.result_status.message}",
        )

        # --- save activation ---
        save_act = mc.save_configuration_activation(
            SaveConfigurationActivationRequestParams(
                configuration_name=config_name,
                start_time=start_time,
                end_time=end_time,
                client_activation_id=client_activation_id,
                description="Integration test activation",
                tags=["integration"],
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            save_act.result_status.is_error,
            f"saveConfigurationActivation failed: {save_act.result_status.message}",
        )
        self.assertEqual(save_act.client_activation_id, client_activation_id)
        self.logger.info("Saved configuration activation: %s", save_act.client_activation_id)

        # --- get by client activation id ---
        get_by_id = mc.get_configuration_activation(client_activation_id=client_activation_id)
        self.assertFalse(
            get_by_id.result_status.is_error,
            f"getConfigurationActivation (by id) failed: {get_by_id.result_status.message}",
        )
        activation = get_by_id.configuration_activation
        self.assertIsNotNone(activation, "getConfigurationActivation should return a record")
        self.assertEqual(activation.configurationName, config_name)
        self.assertEqual(activation.startTime.epochSeconds, start_time)
        self.assertEqual(activation.endTime.epochSeconds, end_time)
        self.logger.info("Retrieved activation by id: %s", activation.clientActivationId)

        # --- get by composite key (configuration_name + start_time) ---
        get_by_key = mc.get_configuration_activation(configuration_name=config_name, start_time=start_time)
        self.assertFalse(
            get_by_key.result_status.is_error,
            f"getConfigurationActivation (by composite key) failed: {get_by_key.result_status.message}",
        )
        self.assertIsNotNone(get_by_key.configuration_activation)
        self.assertEqual(get_by_key.configuration_activation.clientActivationId, client_activation_id)
        self.logger.info("Retrieved activation by composite key")

        # --- query by configuration name ---
        query_act = mc.query_configuration_activations([ConfigurationActivationQuery.configuration_name([config_name])])
        self.assertFalse(
            query_act.result_status.is_error,
            f"queryConfigurationActivations failed: {query_act.result_status.message}",
        )
        queried_ids = [a.clientActivationId for a in query_act.configuration_activations]
        self.assertIn(
            client_activation_id,
            queried_ids,
            "query by configuration name should return the saved activation",
        )
        self.logger.info("Activation query returned %d record(s)", len(query_act.configuration_activations))

        # --- iterate by client activation id ---
        iterated_ids = [
            a.clientActivationId
            for a in mc.iter_configuration_activations(
                [ConfigurationActivationQuery.client_activation_id([client_activation_id])]
            )
        ]
        self.assertIn(
            client_activation_id,
            iterated_ids,
            "iter_configuration_activations should yield the saved activation",
        )
        self.logger.info("Activation iterator yielded %d record(s)", len(iterated_ids))

        # --- getActiveConfigurations for a point inside the interval ---
        active_result = mc.get_active_configurations(timestamp=now)
        self.assertFalse(
            active_result.result_status.is_error,
            f"getActiveConfigurations failed: {active_result.result_status.message}",
        )
        active_ids = [a.clientActivationId for a in active_result.configuration_activations]
        self.assertIn(
            client_activation_id,
            active_ids,
            "getActiveConfigurations should include the activation whose interval covers 'now'",
        )
        self.logger.info(
            "getActiveConfigurations returned %d active record(s)",
            len(active_result.configuration_activations),
        )

        # --- delete activation ---
        delete_act = mc.delete_configuration_activation(client_activation_id=client_activation_id)
        self.assertFalse(
            delete_act.result_status.is_error,
            f"deleteConfigurationActivation failed: {delete_act.result_status.message}",
        )
        self.assertEqual(delete_act.client_activation_id, client_activation_id)
        self.logger.info("Deleted configuration activation: %s", delete_act.client_activation_id)

        # --- get after delete should report a business error ---
        get_after_delete = mc.get_configuration_activation(client_activation_id=client_activation_id)
        self.assertTrue(
            get_after_delete.result_status.is_error,
            "getConfigurationActivation after delete should return an error",
        )
        self.assertIsNone(get_after_delete.configuration_activation)

        # --- clean up the configuration ---
        delete_config = mc.delete_configuration(config_name)
        self.assertFalse(
            delete_config.result_status.is_error,
            f"deleteConfiguration (cleanup) failed: {delete_config.result_status.message}",
        )
        self.logger.info("Machine config activation round-trip integration test completed successfully")

    def test_open_ended_activation_round_trip(self):
        """
        Exercises the open-ended activation shape against real services: an activation saved with no end_time
        must round-trip with endTime genuinely absent (not defaulted to zero), be reported active at an
        arbitrary far-future instant, and stop being active once closed with a real end_time.

        This is the live-bridge pattern: open an interval now, close it when the next change arrives.
        """
        self.assertIsNotNone(self.client.annotation, "annotation client should be initialized")
        mc = self.client.annotation.machine_config

        now = int(time.time())
        config_name = f"INTEGRATION:OPEN-CFG:{now}"
        client_activation_id = f"integration-open-act-{now}"
        start_time = now - 3600
        # Far future rather than "now + delta": the open-ended semantics are the only thing under test,
        # and the probe cannot be perturbed by clock skew or a slow test run.
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)

        self.addCleanup(mc.delete_configuration, config_name)
        self.addCleanup(mc.delete_configuration_activation, client_activation_id)

        save_config = mc.save_configuration(
            SaveConfigurationRequestParams(
                configuration_name=config_name,
                category="integration-open-activation",
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            save_config.result_status.is_error,
            f"saveConfiguration (for open activation) failed: {save_config.result_status.message}",
        )

        # --- save an activation with no end_time: open-ended, "still in effect" ---
        save_act = mc.save_configuration_activation(
            SaveConfigurationActivationRequestParams(
                configuration_name=config_name,
                start_time=start_time,
                client_activation_id=client_activation_id,
                description="Integration test open-ended activation",
                tags=["integration"],
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            save_act.result_status.is_error,
            f"saveConfigurationActivation (open-ended) failed: {save_act.result_status.message}",
        )
        self.logger.info("Saved open-ended configuration activation: %s", save_act.client_activation_id)

        # --- read back: endTime must be genuinely absent, not zero ---
        get_open = mc.get_configuration_activation(client_activation_id=client_activation_id)
        self.assertFalse(
            get_open.result_status.is_error,
            f"getConfigurationActivation (open-ended) failed: {get_open.result_status.message}",
        )
        activation = get_open.configuration_activation
        self.assertIsNotNone(activation, "getConfigurationActivation should return the open-ended record")
        self.assertEqual(activation.startTime.epochSeconds, start_time)
        self.assertFalse(
            activation.HasField("endTime"),
            "an open-ended activation must round-trip with endTime absent, not defaulted to zero",
        )
        self.logger.info("Open-ended activation round-tripped with endTime absent")

        # --- an open-ended activation is active at any instant at or after start_time ---
        active_future = mc.get_active_configurations(timestamp=far_future)
        self.assertFalse(
            active_future.result_status.is_error,
            f"getActiveConfigurations (far future) failed: {active_future.result_status.message}",
        )
        self.assertIn(
            client_activation_id,
            [a.clientActivationId for a in active_future.configuration_activations],
            "getActiveConfigurations should report an open-ended activation as active in the far future",
        )
        self.logger.info("Open-ended activation reported active at %s", far_future.isoformat())

        # --- close it: same client_activation_id means UPDATE, not a second record ---
        end_time = now + 1800
        close_act = mc.save_configuration_activation(
            SaveConfigurationActivationRequestParams(
                configuration_name=config_name,
                start_time=start_time,
                end_time=end_time,
                client_activation_id=client_activation_id,
                description="Integration test open-ended activation",
                tags=["integration"],
                modified_by="dp-python-lib-integration-test",
            )
        )
        self.assertFalse(
            close_act.result_status.is_error,
            f"saveConfigurationActivation (closing) failed: {close_act.result_status.message}",
        )

        get_closed = mc.get_configuration_activation(client_activation_id=client_activation_id)
        self.assertFalse(
            get_closed.result_status.is_error,
            f"getConfigurationActivation (closed) failed: {get_closed.result_status.message}",
        )
        closed = get_closed.configuration_activation
        self.assertIsNotNone(closed, "getConfigurationActivation should return the closed record")
        self.assertTrue(closed.HasField("endTime"), "closing the activation should set endTime")
        self.assertEqual(closed.endTime.epochSeconds, end_time)
        self.logger.info("Closed the open-ended activation at %d", end_time)

        # --- once closed, the far-future probe no longer sees it ---
        active_after_close = mc.get_active_configurations(timestamp=far_future)
        self.assertFalse(
            active_after_close.result_status.is_error,
            f"getActiveConfigurations (after close) failed: {active_after_close.result_status.message}",
        )
        self.assertNotIn(
            client_activation_id,
            [a.clientActivationId for a in active_after_close.configuration_activations],
            "a closed activation should not be active after its end_time",
        )
        self.logger.info("Closed activation correctly absent from far-future active set")

        delete_act = mc.delete_configuration_activation(client_activation_id=client_activation_id)
        self.assertFalse(
            delete_act.result_status.is_error,
            f"deleteConfigurationActivation failed: {delete_act.result_status.message}",
        )

        delete_config = mc.delete_configuration(config_name)
        self.assertFalse(
            delete_config.result_status.is_error,
            f"deleteConfiguration (cleanup) failed: {delete_config.result_status.message}",
        )
        self.logger.info("Open-ended activation integration test completed successfully")


if __name__ == "__main__":
    unittest.main(verbosity=2)
