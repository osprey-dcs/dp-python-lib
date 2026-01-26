## Overview

We are creating an API client for calling gRPC API service methods.  The top-level client object MldpClient is defined in mldp_client.py.  It will use an IngestionClient object to call the methods on the Ingestion Service API methods.

The dp_python_lib/client directory contains the Python stubs generated for the gRPC API.

The IngestionClient class provides wrapper methods for calling gRPC API methods on the DpIngestionService.  We are going to be implementing those wrapper methods, one step at a time.  The tasks are sketched out in the subsequent "Tasks" section of this document.

## Tasks

1.0 Build RegisterProviderRequest API object.  Before we can call the registerProvider() API method, we need to create a RegisterProviderRequest API object.  The user of our IngestionClient will create a RegisterProviderRequestParams object containing name, description, tag_list, and attribute_map.  It will then call IngestionClient.register_provider() with that RegisterProviderRequestParams object. The register_provider() method will call _build_register_provider_request() with the RegisterProviderRequestParams object.   The _build_register_provider_request() method will create and return an API RegisterProviderRequest object from the supplied params object. Please implement IngestionClient._build_register_provider_request() to create and return a API RegisterProviderRequest object for the supplied RegisterProviderRequestParams object.

1.0.1 Please add unit test coverage for the new _build_register_provider_request() method.

2.0 Next we will implement IngestionClient._send_register_provider.  The method exists but only creates a stub.  Please fully implement the method to call the registerProvider() API method with the supplied RegisterProviderRequest object, and return a RegisterProviderApiResult object.  The result isError flag should be set if the API call fails along with the corresponding error message in the result object's status object.  If the API call succeeds, the RegisterProviderResponse object should be added to the result object. In Java, the implementation of this method looks as follows:
```
        final DpIngestionServiceGrpc.DpIngestionServiceStub asyncStub =
                DpIngestionServiceGrpc.newStub(channel);

        final RegisterProviderResponseObserver responseObserver =
                new RegisterProviderResponseObserver();

        // send request in separate thread to better simulate out of process grpc,
        // otherwise service handles request in this thread
        new Thread(() -> {
            asyncStub.registerProvider(request, responseObserver);
        }).start();

        responseObserver.await();

        if (responseObserver.isError()) {
            return new RegisterProviderApiResult(true, responseObserver.getErrorMessage());
        } else {
            return new RegisterProviderApiResult(responseObserver.getResponseList().get(0));
        }
```

