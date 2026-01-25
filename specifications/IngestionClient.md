## Overview

We are creating an API client for calling gRPC API service methods.  The top-level client object MldpClient is defined in mldp_client.py.  It will use an IngestionClient object to call the methods on the Ingestion Service API methods.

The dp_python_lib/client directory contains the Python stubs generated for the gRPC API.

The IngestionClient class provides wrapper methods for calling gRPC API methods on the DpIngestionService.  We are going to be implementing those wrapper methods, one step at a time.  The tasks are sketched out in the subsequent "Tasks" section of this document.

## Tasks

1.0 Build RegisterProviderRequest API object.  Before we can call the registerProvider() API method, we need to create a RegisterProviderRequest API object.  The user of our IngestionClient will create a RegisterProviderRequestParams object containing name, description, tag_list, and attribute_map.  It will then call IngestionClient.register_provider() with that RegisterProviderRequestParams object. The register_provider() method will call _build_register_provider_request() with the RegisterProviderRequestParams object.   The _build_register_provider_request() method will create and return an API RegisterProviderRequest object from the supplied params object. Please implement IngestionClient._build_register_provider_request() to create and return a API RegisterProviderRequest object for the supplied RegisterProviderRequestParams object.

1.0.1 Please add unit test coverage for the new _build_register_provider_request() method.

