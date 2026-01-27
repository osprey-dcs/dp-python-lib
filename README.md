## Overview

This repo contains a python client API library for the [Machine Learning Data Platform](https://github.com/osprey-dcs/data-platform) (MLDP) project.  The gRPC API definition for utilizing the MLDP services is defined in the [dp-grpc repo]([https://github.com/osprey-dcs/dp-grpc](https://github.com/craigmcchesney/dp-grpc)).

NOTE: The dp-grpc repo includes an Actions workflow (generate-python-stubs.yml) for generating Python stubs from the API definition.  It can be triggered manually, as part of the development process, and is triggered automatically when a new release tag is created (e.g., a tag prefixed with "rel-").  The workflow creates a pull request to merge the files to this dp-python-lib repo, in the [src/dp_python_lib/grpc](src/dp_python_lib/grpc) directory.  Because the files are generated, they should not be edited manually.  Any required changes should be made to the process that generates the stubs, not the generated files themselves.

NOTE: This repo is a work in progess and requires additional work before it is useful for building Python client applications!

## Status

The goal for the first phase of this project was to build the framework necessary to handle a single gRPC API call and in the process to develop strategies / patterns for gRPC stub generation, service / API / client / application abstractions, gRPC communication, configuration, logging, Python conventions, unit testing, and integration testing.  Having accomplished this goal, the next phase of the project will focus on 1) adding handling for additional MLDP service APIs and 2) designing and implementing higher-level mechanisms for building pipelines for machine learning applications.  A high-level TODO list is below.

## Key Classes

The primary user-facing class in the framework is [MldpClient](src/dp_python_lib/client/mldp_client.py).  This class provides simple wrappers for calling the MLDP service APIs needed to build a Python client.  A second user-facing class, mldp_application.py, will be added that provides higher level features on top of the APIs that will be useful for building applications that are part of a machine learning data pipeline.

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

This same pattern will be utilized for calling all the various service APIs.  The intention of the MldpClient class is to hide the details of the gRPC APIs to the extent that is possible.  A good place to look for additional examples is in the [integration test directory](tests/integration).

## TODO

* Implement additional API wrappers:
  * Ingestion Service
    * ingestData() - At least a simple implementation of unary ingestion, since it is not envisioned that Python clients will be used for high-volume ingestion.
    * queryRequestStatus() - Checks async status of data ingestion requests.
    * subscribeData() - Receives data for specified PVs from the ingestion stream.
  * Query Service
    * queryData() - Retrieves bucketed PV time-series data.
    * queryTable() - Retrieves PV time-series data in tabular format.
    * queryPvMetadata() - Retrieves ingestion metadata for PVs.
    * queryProviders() - Retrieves Provider information.
    * queryProviderMetadata() - Retrieves ingestion metadata for providers.
  * Annotation Service
    * saveDataSet() - Creates or saves a dataset including a collection of PVs and time ranges.
    * queryDataSets() - Retrieves saved datasets.
    * saveAnnotation() - Creates or saves an annotation targeting a dataset.
    * queryAnnotations() - Retrieves saved annotations.
    * exportData() - Exports datasets to popular file formats.
  * Ingestion Stream Service
    * subscribeDataEvent() - Registers to receive notification when a data condition in the ingestion stream is triggered.
   
