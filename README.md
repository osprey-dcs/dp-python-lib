## Overview

This repo contains a python client API library for the [Machine Learning Data Platform](https://github.com/osprey-dcs/data-platform) (MLDP) project.  The gRPC API definition for utilizing the MLDP services is defined in the [dp-grpc repo]([https://github.com/osprey-dcs/dp-grpc](https://github.com/craigmcchesney/dp-grpc)).

NOTE: The dp-grpc repo includes an Actions workflow (generate-python-stubs.yml) for generating Python stubs from the API definition.  It can be triggered manually, as part of the development process, and is triggered automatically when a new release tag is created (e.g., a tag prefixed with "rel-").  The workflow creates a pull request to merge the files to this dp-python-lib repo, in the [src/dp_python_lib/grpc](src/dp_python_lib/grpc) directory.  Because the files are generated, they should not be edited manually.  Any required changes should be made to the process that generates the stubs, not the generated files themselves.

NOTE: This repo is a work in progress and requires additional work before it is useful for building Python client applications!

## Documentation

The **[cookbook](doc/cookbook/)** is the task-oriented guide to using this library: connecting a
client, cataloguing PVs, recording machine configuration, and querying time-series data into
pandas or NumPy.  Start with [API conventions](doc/cookbook/conventions.md) and
[Creating and connecting a client](doc/cookbook/connecting.md).

For the wire protocol beneath this library — the protobuf messages and RPC semantics, documented
in Java — see the [dp-grpc cookbook](https://github.com/osprey-dcs/dp-grpc/tree/main/doc/cookbook).

## Status

The goal for the first phase of this project was to build the framework necessary to handle a single gRPC API call and in the process to develop strategies / patterns for gRPC stub generation, service / API / client / application abstractions, gRPC communication, configuration, logging, Python conventions, unit testing, and integration testing.  Having accomplished this goal, the next phase of the project will focus on 1) adding handling for additional MLDP service APIs and 2) designing and implementing higher-level mechanisms for building pipelines for machine learning applications.  A high-level TODO list is below.

## Key Classes

The primary user-facing class in the framework is [MldpClient](src/dp_python_lib/client/mldp_client.py).  That class reads the configuration, initializes the API, and creates interface objects for accessing each of the four MLDP services: IngestionClient, QueryClient, AnnotationClient, and IngestionStreamClient.  These classes provide simple wrappers and supporting data structures for calling the MLDP service APIs.  A second user-facing class, MldpApplication, will be added that provides higher level features on top of the APIs that will be useful for building applications that are part of a machine learning data pipeline.

## Usage Examples

A simple example for calling the Ingestion Service registerProvider() API method is shown below:
```
        cls.client = MldpClient()

        params = RegisterProviderRequestParams(
            name=unique_name,
            description="Integration test provider for dp-python-lib",
            tag_list=["integration", "test", "automated"],
            attribute_map={
                "test_type": "integration", 
                "framework": "unittest",
                "timestamp": str(timestamp),
                "client": "dp-python-lib"
            }
        )
        
        result = self.client.ingestion_client.register_provider(params)
```

The Annotation Service PV metadata API is accessed via the `annotation` facade, which groups the feature-scoped clients that share the Annotation Service connection.  PV metadata methods are exposed under `client.annotation.pv_metadata`:
```
        client = MldpClient()
        pv_client = client.annotation.pv_metadata

        # Save metadata for a PV
        save_params = SavePvMetadataRequestParams(
            pv_name="ABC:1",
            aliases=["abc-one"],
            tags=["vacuum", "beam"],
            attributes={"unit": "V", "system": "vacuum"},
            modified_by="operator",
            description="Vacuum gauge readback",
        )
        save_result = pv_client.save_pv_metadata(save_params)

        # Get metadata by PV name or alias
        get_result = pv_client.get_pv_metadata("abc-one")
        metadata = get_result.pv_metadata

        # Query metadata using criterion helpers; iterate transparently across pages
        from dp_python_lib.client import PvMetadataQuery as Q
        for pv in pv_client.iter_pv_metadata([Q.pv_name(prefix=["ABC:"]), Q.tags(["vacuum"])]):
            print(pv.pvName)

        # Delete metadata by PV name or alias
        delete_result = pv_client.delete_pv_metadata("ABC:1")
```

The Annotation Service machine configuration API is exposed under `client.annotation.machine_config`.  It manages named machine *configurations* and their temporal *activations* (which configuration was active over a given time interval), plus a point-in-time "what is active now" lookup:
```
        from datetime import datetime, timezone
        from dp_python_lib.client import (
            SaveConfigurationRequestParams,
            SaveConfigurationActivationRequestParams,
            ConfigurationQuery as C,
            ConfigurationActivationQuery as CA,
        )

        mc = client.annotation.machine_config

        # Save a configuration
        mc.save_configuration(SaveConfigurationRequestParams(
            configuration_name="beamline-optics",
            category="optics",
            tags=["production"],
            attributes={"owner": "ops"},
            modified_by="operator",
        ))

        # Get / query / iterate configurations
        config = mc.get_configuration("beamline-optics").configuration
        for cfg in mc.iter_configurations([C.name(prefix=["beamline-"]), C.tags(["production"])]):
            print(cfg.configurationName)

        # Record that the configuration was active over a time interval.  Timestamps accept a
        # timezone-aware datetime, epoch seconds, or a common.Timestamp.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        mc.save_configuration_activation(SaveConfigurationActivationRequestParams(
            configuration_name="beamline-optics",
            start_time=start,
            end_time=end,
            client_activation_id="act-001",
            modified_by="operator",
        ))

        # Get an activation by client id, or by the (configuration_name, start_time) composite key
        act = mc.get_configuration_activation(client_activation_id="act-001").configuration_activation
        act = mc.get_configuration_activation(
            configuration_name="beamline-optics", start_time=start).configuration_activation

        # Query / iterate activations
        for a in mc.iter_configuration_activations([CA.configuration_name(["beamline-optics"])]):
            print(a.clientActivationId)

        # What configurations are active right now? (pass a timestamp for a historical instant)
        active = mc.get_active_configurations().configuration_activations

        # Delete
        mc.delete_configuration_activation(client_activation_id="act-001")
        mc.delete_configuration("beamline-optics")
```

### Query Service — v2 time-series data (sample-oriented)

The sample-oriented v2 query methods are exposed at `client.query`.  A query is a kind-neutral `QueryParams`
over a half-open time range `[begin, end)`, built from the `PvQuery` (`PV`) and `ConfigQuery` (`CFG`) criterion
helpers.  Low-level methods return the raw protobuf `ColumnTable`; the Pythonic conversions (DataFrame / NumPy /
Excel) require the optional `[analysis]` extra: `pip install dp-python-lib[analysis]`.

```python
        from datetime import datetime, timezone
        from dp_python_lib.client import MldpClient, QueryParams, PvQuery as PV, ConfigQuery as CFG

        client = MldpClient()
        q = client.query

        begin = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)

        # Choose at most one PV selector form: a name list, a name pattern, or a metadata query.
        params = QueryParams(
            begin_time=begin, end_time=end,
            pv_selector=PV.metadata([PV.pv_name(prefix=["ABC:"]), PV.tags(["vacuum"])]),
            config_criteria=[CFG.configuration_name(["beamline-optics"])],   # optional; restricts to active intervals
            limit=1000,                                                       # rows PER PAGE, not a total cap
        )

        # unary: one page, or transparently page across the whole range (raises RuntimeError on a page error)
        for page in q.iter_query_samples(params):
            table = page.column_table

        # server-streaming: lazy, fire-and-consume, no page tokens (raises RuntimeError on a mid-stream error)
        for page in q.iter_query_samples_stream(params):
            table = page.column_table

        # Pythonic conversions (require the [analysis] extra)
        df = q.query_samples(params).to_dataframe()      # one page -> pandas.DataFrame (UTC datetime index)

        from dp_python_lib.client import query_conversions as qc
        df_all = qc.query_samples_to_dataframe(q, params, max_rows=1_000_000)  # pages internally, concats by column name
        qc.dataframe_to_excel(df_all, "out.xlsx")
```

This same pattern will be utilized for calling all the various service APIs.  The intention of the MldpClient class is to hide the details of the gRPC APIs to the extent that is possible.  A good place to look for additional examples is in the [integration test directory](tests/integration).

## TODO

* Implement additional API wrappers:
  * Ingestion Service
    * ingestData() / ingestDataStream() / ingestDataBidiStream() - Full ingestion client (shared DataFrame payload model + unary and streaming). Tracked as issue #17; also unblocks the closed-loop query integration test.
    * queryRequestStatus() - Checks async status of data ingestion requests.
    * subscribeData() - Receives data for specified PVs from the ingestion stream.
  * Query Service
    * querySamples() / querySamplesStream() - Retrieves PV samples - DONE (client.query): unary (resumable paging) and server-streaming, plus DataFrame/NumPy/Excel conversions via the optional [analysis] extra.
    * queryBuckets() / queryBucketsStream() - Retrieves raw data buckets. Tracked as issue #16.
    * queryData() - Retrieves bucketed PV time-series data.
    * queryTable() - Retrieves PV time-series data in tabular format.
    * queryPvStats() - Retrieves archive ingestion statistics for PVs (renamed from queryPvMetadata(); note user-defined PV metadata is now served by DpAnnotationService, see below).
    * queryProviders() - Retrieves Provider information.
    * queryProviderStats() - Retrieves archive ingestion statistics for providers (renamed from queryProviderMetadata()).
  * Annotation Service
    * PV metadata API - DONE (client.annotation.pv_metadata): savePvMetadata(), getPvMetadata(), queryPvMetadata(), deletePvMetadata().
    * Machine configuration API - DONE (client.annotation.machine_config): saveConfiguration(), getConfiguration(), queryConfigurations(), deleteConfiguration(), saveConfigurationActivation(), getConfigurationActivation(), queryConfigurationActivations(), deleteConfigurationActivation(), getActiveConfigurations().
    * saveDataSet() - Creates or saves a dataset including a collection of PVs and time ranges.
    * queryDataSets() - Retrieves saved datasets.
    * saveAnnotation() - Creates or saves an annotation targeting a dataset.
    * queryAnnotations() - Retrieves saved annotations.
    * exportData() - Exports datasets to popular file formats.
  * Ingestion Stream Service
    * subscribeDataEvent() - Registers to receive notification when a data condition in the ingestion stream is triggered. 
* Design and implement MldpApplication with high-level application support.
* Create CI workflow(s) for publishing running regession tests and publishing release artifacts.
   
