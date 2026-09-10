import os
import sys
import unittest
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from assignment_spy import watch_assignments

from dp_python_lib.client.annotations_client import (
    AnnotationQuery,
    AnnotationsClient,
    DeleteAnnotationApiResult,
    GetAnnotationApiResult,
    GetCalculationsApiResult,
    QueryAnnotationsApiResult,
    SaveAnnotationApiResult,
    SaveAnnotationRequestParams,
    calculations,
)
from dp_python_lib.grpc import annotation_pb2, common_pb2


def _response_with_field(field_name):
    """
    Build a Mock response whose HasField(field) returns True only for field_name.  This keeps both the _send_*
    oneof check and the *ApiResult property accessors (which also call HasField) consistent.
    """
    response = Mock()
    response.HasField = Mock(side_effect=lambda field: field == field_name)
    return response


def _frame(column_name="x_rms"):
    """A minimal common.DataFrame carrying one named double column, for calculations construction tests."""
    frame = common_pb2.DataFrame()
    column = frame.doubleColumns.add()
    column.name = column_name
    column.values[:] = [1.0, 2.0]
    return frame


class TestCalculationsBuilder(unittest.TestCase):
    """Unit tests for the calculations() builder."""

    def test_builds_one_frame_per_entry(self):
        result = calculations({"bpm-statistics": _frame(), "rf-statistics": _frame("y_rms")})

        self.assertEqual(len(result.calculationDataFrames), 2)
        self.assertEqual(
            {f.name for f in result.calculationDataFrames},
            {"bpm-statistics", "rf-statistics"},
        )

    def test_carries_frame_content(self):
        result = calculations({"f1": _frame("x_rms")})

        frame = result.calculationDataFrames[0].frame
        self.assertEqual([c.name for c in frame.doubleColumns], ["x_rms"])
        self.assertEqual(list(frame.doubleColumns[0].values), [1.0, 2.0])

    def test_rejects_empty_frames(self):
        with self.assertRaises(ValueError) as ctx:
            calculations({})
        self.assertIn("at least one frame", str(ctx.exception))

    def test_rejects_empty_frame_name(self):
        with self.assertRaises(ValueError) as ctx:
            calculations({"": _frame()})
        self.assertIn("non-empty name", str(ctx.exception))


class TestAnnotationsClientBuildRequests(unittest.TestCase):
    """Unit tests for the request-building helpers (no gRPC calls)."""

    def setUp(self):
        self.client = AnnotationsClient(Mock())

    def test_build_save_request_all_fields(self):
        calcs = calculations({"f1": _frame()})
        params = SaveAnnotationRequestParams(
            name="BPM X RMS",
            owner_id="cmcchesney",
            dataset_ids=["ds-1", "ds-2"],
            annotation_ids=["an-9"],
            description="a test annotation",
            tags=["reviewed"],
            attributes={"runNumber": "4471"},
            modified_by="tester",
            calculations=calcs,
            annotation_id="an-1",
        )
        request = self.client._build_save_annotation_request(params)

        self.assertEqual(request.id, "an-1")
        self.assertEqual(request.name, "BPM X RMS")
        self.assertEqual(request.ownerId, "cmcchesney")
        self.assertEqual(list(request.dataSetIds), ["ds-1", "ds-2"])
        self.assertEqual(list(request.annotationIds), ["an-9"])
        self.assertEqual(request.description, "a test annotation")
        self.assertEqual(list(request.tags), ["reviewed"])
        self.assertEqual(request.modifiedBy, "tester")
        self.assertEqual({(a.name, a.value) for a in request.attributes}, {("runNumber", "4471")})
        self.assertTrue(request.HasField("calculations"))
        self.assertEqual([f.name for f in request.calculations.calculationDataFrames], ["f1"])

    def test_build_save_request_required_fields_only(self):
        params = SaveAnnotationRequestParams(name="n", owner_id="o", dataset_ids=["ds-1"])
        request = self.client._build_save_annotation_request(params)

        self.assertEqual(request.name, "n")
        self.assertEqual(request.ownerId, "o")
        self.assertEqual(list(request.dataSetIds), ["ds-1"])
        self.assertEqual(request.id, "")
        self.assertEqual(list(request.annotationIds), [])
        self.assertEqual(request.description, "")
        self.assertEqual(list(request.tags), [])
        self.assertEqual(len(request.attributes), 0)
        self.assertEqual(request.modifiedBy, "")

    def test_build_save_request_omitted_calculations_is_absent(self):
        # Absent, not an empty message: omitting calculations on a replace is what CLEARS the stored object, so the
        # field must genuinely not be set rather than carry an empty Calculations.
        params = SaveAnnotationRequestParams(name="n", owner_id="o", dataset_ids=["ds-1"])
        request = self.client._build_save_annotation_request(params)
        self.assertFalse(request.HasField("calculations"))

    def test_build_save_request_omitted_id_is_not_assigned(self):
        params = SaveAnnotationRequestParams(name="n", owner_id="o", dataset_ids=["ds-1"])
        with watch_assignments(annotation_pb2, "SaveAnnotationRequest", "id") as assigned:
            self.client._build_save_annotation_request(params)
        self.assertEqual(assigned, [], "an omitted annotation_id must not be assigned")

    def test_build_get_request(self):
        self.assertEqual(self.client._build_get_annotation_request("an-1").annotationId, "an-1")

    def test_build_delete_request(self):
        self.assertEqual(self.client._build_delete_annotation_request("an-1").annotationId, "an-1")

    def test_build_get_calculations_request(self):
        self.assertEqual(self.client._build_get_calculations_request("calc-1").calculationsId, "calc-1")

    def test_build_query_request(self):
        criteria = [AnnotationQuery.tags(["reviewed"]), AnnotationQuery.datasets(["ds-1"])]
        request = self.client._build_query_annotations_request(criteria, limit=25, page_token="tok")

        self.assertEqual(len(request.criteria), 2)
        self.assertTrue(request.criteria[0].HasField("tagsCriterion"))
        self.assertTrue(request.criteria[1].HasField("dataSetsCriterion"))
        self.assertEqual(list(request.criteria[1].dataSetsCriterion.dataSetIds), ["ds-1"])
        self.assertEqual(request.limit, 25)
        self.assertEqual(request.pageToken, "tok")

    def test_build_query_request_no_criteria(self):
        self.assertEqual(len(self.client._build_query_annotations_request().criteria), 0)

    def test_build_query_request_limit_zero_is_set(self):
        # limit=0 must be forwarded (distinct from "not provided"); guard against a truthiness regression (#13).
        with watch_assignments(annotation_pb2, "QueryAnnotationsRequest", "limit") as assigned:
            self.client._build_query_annotations_request([AnnotationQuery.tags(["x"])], limit=0)
        self.assertEqual(assigned, [0], "limit=0 must be assigned, not dropped by a truthiness guard")

    def test_build_query_request_limit_omitted_is_unset(self):
        with watch_assignments(annotation_pb2, "QueryAnnotationsRequest", "limit") as assigned:
            self.client._build_query_annotations_request([AnnotationQuery.tags(["x"])])
        self.assertEqual(assigned, [], "an omitted limit must not be assigned")

    def test_build_query_request_empty_page_token_is_unset(self):
        with watch_assignments(annotation_pb2, "QueryAnnotationsRequest", "pageToken") as assigned:
            self.client._build_query_annotations_request([AnnotationQuery.tags(["x"])], page_token="")
        self.assertEqual(assigned, [], "an empty page token must not be assigned")


class TestSaveAnnotationRequestParamsValidation(unittest.TestCase):
    """
    Unit tests for the params validation.  The server requires all three, so catching them here turns a round trip
    into an immediate error naming the field.
    """

    def test_rejects_empty_name(self):
        with self.assertRaises(ValueError) as ctx:
            SaveAnnotationRequestParams(name="", owner_id="cmcchesney", dataset_ids=["ds-1"])
        self.assertIn("name", str(ctx.exception))

    def test_rejects_empty_owner_id(self):
        with self.assertRaises(ValueError) as ctx:
            SaveAnnotationRequestParams(name="orbit drift", owner_id="", dataset_ids=["ds-1"])
        self.assertIn("owner_id", str(ctx.exception))

    def test_rejects_empty_dataset_ids(self):
        with self.assertRaises(ValueError) as ctx:
            SaveAnnotationRequestParams(name="orbit drift", owner_id="cmcchesney", dataset_ids=[])
        self.assertIn("dataset_ids", str(ctx.exception))

    def test_accepts_required_fields(self):
        params = SaveAnnotationRequestParams(name="orbit drift", owner_id="cmcchesney", dataset_ids=["ds-1"])
        self.assertEqual(params.dataset_ids, ["ds-1"])


class TestAnnotationQueryHelpers(unittest.TestCase):
    """Unit tests for the AnnotationQuery criterion builders."""

    def test_ids(self):
        c = AnnotationQuery.ids(["a"])
        self.assertTrue(c.HasField("idCriterion"))
        self.assertEqual(list(c.idCriterion.ids), ["a"])

    def test_owners(self):
        c = AnnotationQuery.owners(["o1"])
        self.assertTrue(c.HasField("ownerCriterion"))
        self.assertEqual(list(c.ownerCriterion.ownerIds), ["o1"])

    def test_datasets(self):
        c = AnnotationQuery.datasets(["ds-1"])
        self.assertTrue(c.HasField("dataSetsCriterion"))
        self.assertEqual(list(c.dataSetsCriterion.dataSetIds), ["ds-1"])

    def test_annotations(self):
        c = AnnotationQuery.annotations(["an-1"])
        self.assertTrue(c.HasField("annotationsCriterion"))
        self.assertEqual(list(c.annotationsCriterion.annotationIds), ["an-1"])

    def test_name_all_options(self):
        c = AnnotationQuery.name(exact=["A"], prefix=["B"], contains=["C"])
        self.assertTrue(c.HasField("nameCriterion"))
        self.assertEqual(list(c.nameCriterion.exact), ["A"])
        self.assertEqual(list(c.nameCriterion.prefix), ["B"])
        self.assertEqual(list(c.nameCriterion.contains), ["C"])

    def test_text(self):
        c = AnnotationQuery.text("rms")
        self.assertTrue(c.HasField("textCriterion"))
        self.assertEqual(c.textCriterion.text, "rms")

    def test_tags(self):
        c = AnnotationQuery.tags(["t1"])
        self.assertTrue(c.HasField("tagsCriterion"))
        self.assertEqual(list(c.tagsCriterion.values), ["t1"])

    def test_attributes_with_values(self):
        c = AnnotationQuery.attributes("runNumber", ["4471"])
        self.assertTrue(c.HasField("attributesCriterion"))
        self.assertEqual(c.attributesCriterion.key, "runNumber")
        self.assertEqual(list(c.attributesCriterion.values), ["4471"])

    def test_attributes_key_only(self):
        for criterion in (AnnotationQuery.attributes("runNumber"), AnnotationQuery.attributes("runNumber", [])):
            self.assertTrue(criterion.HasField("attributesCriterion"))
            self.assertEqual(criterion.attributesCriterion.key, "runNumber")
            self.assertEqual(list(criterion.attributesCriterion.values), [])

    def test_empty_inputs_raise(self):
        with self.assertRaises(ValueError):
            AnnotationQuery.ids([])
        with self.assertRaises(ValueError):
            AnnotationQuery.owners([])
        with self.assertRaises(ValueError):
            AnnotationQuery.datasets([])
        with self.assertRaises(ValueError):
            AnnotationQuery.annotations([])
        with self.assertRaises(ValueError):
            AnnotationQuery.name()
        with self.assertRaises(ValueError):
            AnnotationQuery.text("")
        with self.assertRaises(ValueError):
            AnnotationQuery.tags([])
        with self.assertRaises(ValueError):
            AnnotationQuery.attributes("")


class TestQueryAnnotationsTextCriterionRule(unittest.TestCase):
    """Two text criteria cannot be ANDed, so the client rejects them before the RPC."""

    def setUp(self):
        self.client = AnnotationsClient(Mock())
        self.client._stub = Mock()

    def test_two_text_criteria_raise(self):
        with self.assertRaises(ValueError) as ctx:
            self.client.query_annotations([AnnotationQuery.text("a"), AnnotationQuery.text("b")])
        self.assertIn("at most one text criterion", str(ctx.exception))
        self.client._stub.queryAnnotations.assert_not_called()

    def test_one_text_criterion_is_allowed(self):
        response = _response_with_field("annotationsResult")
        response.annotationsResult = annotation_pb2.QueryAnnotationsResponse.AnnotationsResult()
        self.client._stub.queryAnnotations.return_value = response

        result = self.client.query_annotations([AnnotationQuery.text("a"), AnnotationQuery.tags(["t"])])

        self.assertFalse(result.result_status.is_error)

    def test_iter_annotations_also_rejects_two_text_criteria(self):
        with self.assertRaises(ValueError):
            list(self.client.iter_annotations([AnnotationQuery.text("a"), AnnotationQuery.text("b")]))


class TestSendSaveAnnotation(unittest.TestCase):
    def setUp(self):
        self.client = AnnotationsClient(Mock())
        self.request = annotation_pb2.SaveAnnotationRequest(name="n", ownerId="o")

    def test_success(self):
        response = _response_with_field("saveAnnotationResult")
        response.saveAnnotationResult.annotationId = "an-1"
        response.saveAnnotationResult.calculationsId = "calc-1"
        mock_stub = Mock()
        mock_stub.saveAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_annotation(self.request)

        self.assertIsInstance(result, SaveAnnotationApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.annotation_id, "an-1")
        self.assertEqual(result.calculations_id, "calc-1")
        mock_stub.saveAnnotation.assert_called_once_with(self.request)

    def test_no_calculations_yields_empty_string_not_none(self):
        # "" means the request carried no calculations; None is reserved for a failed call.
        response = _response_with_field("saveAnnotationResult")
        response.saveAnnotationResult.annotationId = "an-1"
        response.saveAnnotationResult.calculationsId = ""
        mock_stub = Mock()
        mock_stub.saveAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_annotation(self.request)

        self.assertEqual(result.calculations_id, "")
        self.assertIsNotNone(result.calculations_id)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "dataSetIds must not be empty"
        mock_stub = Mock()
        mock_stub.saveAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "dataSetIds must not be empty")
        self.assertIsNone(result.annotation_id)
        self.assertIsNone(result.calculations_id)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.saveAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="Connection timeout")
        mock_stub.saveAnnotation.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_save_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: Connection timeout", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.saveAnnotation.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_save_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestSendGetAnnotation(unittest.TestCase):
    def setUp(self):
        self.client = AnnotationsClient(Mock())
        self.request = annotation_pb2.GetAnnotationRequest(annotationId="an-1")

    def _response_with_annotation(self, annotation):
        response = _response_with_field("getAnnotationResult")
        response.getAnnotationResult.annotation = annotation
        return response

    def test_success(self):
        annotation = annotation_pb2.Annotation(id="an-1", name="BPM X RMS")
        mock_stub = Mock()
        mock_stub.getAnnotation.return_value = self._response_with_annotation(annotation)
        self.client._stub = mock_stub

        result = self.client._send_get_annotation(self.request)

        self.assertIsInstance(result, GetAnnotationApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.annotation.name, "BPM X RMS")
        mock_stub.getAnnotation.assert_called_once_with(self.request)

    def test_calculations_are_returned_inline(self):
        annotation = annotation_pb2.Annotation(id="an-1", calculationsId="calc-1")
        annotation.calculations.CopyFrom(calculations({"f1": _frame()}))
        mock_stub = Mock()
        mock_stub.getAnnotation.return_value = self._response_with_annotation(annotation)
        self.client._stub = mock_stub

        result = self.client._send_get_annotation(self.request)

        self.assertIsNotNone(result.calculations)
        self.assertEqual([f.name for f in result.calculations.calculationDataFrames], ["f1"])

    def test_calculations_none_when_annotation_has_none(self):
        annotation = annotation_pb2.Annotation(id="an-1")
        mock_stub = Mock()
        mock_stub.getAnnotation.return_value = self._response_with_annotation(annotation)
        self.client._stub = mock_stub

        result = self.client._send_get_annotation(self.request)

        self.assertIsNone(result.calculations)
        self.assertIsNotNone(result.annotation)

    def test_not_found_is_a_business_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "no Annotation record found for id: an-1"
        mock_stub = Mock()
        mock_stub.getAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.annotation)
        self.assertIsNone(result.calculations)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.getAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.getAnnotation.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_get_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.getAnnotation.side_effect = KeyError("bad")
        self.client._stub = mock_stub

        result = self.client._send_get_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error", result.result_status.message)


class TestSendQueryAnnotations(unittest.TestCase):
    def setUp(self):
        self.client = AnnotationsClient(Mock())
        self.request = annotation_pb2.QueryAnnotationsRequest()

    def _result(self, names, next_token=""):
        result = annotation_pb2.QueryAnnotationsResponse.AnnotationsResult()
        for name in names:
            result.annotations.append(annotation_pb2.Annotation(name=name))
        result.nextPageToken = next_token
        return result

    def test_success(self):
        response = _response_with_field("annotationsResult")
        response.annotationsResult = self._result(["a", "b"], "tok")
        mock_stub = Mock()
        mock_stub.queryAnnotations.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_annotations(self.request)

        self.assertIsInstance(result, QueryAnnotationsApiResult)
        self.assertEqual([a.name for a in result.annotations], ["a", "b"])
        self.assertEqual(result.next_page_token, "tok")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "invalid page token"
        mock_stub = Mock()
        mock_stub.queryAnnotations.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_annotations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.annotations, [])
        self.assertEqual(result.next_page_token, "")

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.queryAnnotations.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_annotations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.queryAnnotations.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_query_annotations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.queryAnnotations.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_query_annotations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestSendDeleteAnnotation(unittest.TestCase):
    def setUp(self):
        self.client = AnnotationsClient(Mock())
        self.request = annotation_pb2.DeleteAnnotationRequest(annotationId="an-1")

    def test_success(self):
        response = _response_with_field("deleteAnnotationResult")
        response.deleteAnnotationResult.annotationId = "an-1"
        mock_stub = Mock()
        mock_stub.deleteAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_annotation(self.request)

        self.assertIsInstance(result, DeleteAnnotationApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.annotation_id, "an-1")
        mock_stub.deleteAnnotation.assert_called_once_with(self.request)

    def test_not_found_is_a_business_error(self):
        # Deleting an annotation that does not exist is a REJECT, not a silent success.
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "no Annotation record found for id: an-1"
        mock_stub = Mock()
        mock_stub.deleteAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("no Annotation record found", result.result_status.message)
        self.assertIsNone(result.annotation_id)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.deleteAnnotation.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.deleteAnnotation.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_delete_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.deleteAnnotation.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_delete_annotation(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestSendGetCalculations(unittest.TestCase):
    def setUp(self):
        self.client = AnnotationsClient(Mock())
        self.request = annotation_pb2.GetCalculationsRequest(calculationsId="calc-1")

    def test_success(self):
        response = _response_with_field("getCalculationsResult")
        response.getCalculationsResult.calculations = calculations({"f1": _frame()})
        mock_stub = Mock()
        mock_stub.getCalculations.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_calculations(self.request)

        self.assertIsInstance(result, GetCalculationsApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual([f.name for f in result.calculations.calculationDataFrames], ["f1"])
        mock_stub.getCalculations.assert_called_once_with(self.request)

    def test_not_found_is_a_business_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "no Calculations record found for id: calc-1"
        mock_stub = Mock()
        mock_stub.getCalculations.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_calculations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.calculations)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.getCalculations.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_calculations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.getCalculations.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_get_calculations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.getCalculations.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_get_calculations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestIterAnnotations(unittest.TestCase):
    def setUp(self):
        self.client = AnnotationsClient(Mock())
        self.criteria = [AnnotationQuery.tags(["reviewed"])]

    def _page(self, names, next_token):
        response = _response_with_field("annotationsResult")
        result = annotation_pb2.QueryAnnotationsResponse.AnnotationsResult()
        for name in names:
            result.annotations.append(annotation_pb2.Annotation(name=name))
        result.nextPageToken = next_token
        response.annotationsResult = result
        return response

    def test_pages_through_all_results(self):
        mock_stub = Mock()
        mock_stub.queryAnnotations.side_effect = [self._page(["a", "b"], "tok1"), self._page(["c"], "")]
        self.client._stub = mock_stub

        names = [a.name for a in self.client.iter_annotations(self.criteria, limit=2)]

        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(mock_stub.queryAnnotations.call_count, 2)
        second_request = mock_stub.queryAnnotations.call_args_list[1].args[0]
        self.assertEqual(second_request.pageToken, "tok1")

    def test_single_page(self):
        mock_stub = Mock()
        mock_stub.queryAnnotations.return_value = self._page(["a"], "")
        self.client._stub = mock_stub

        self.assertEqual([a.name for a in self.client.iter_annotations(self.criteria)], ["a"])
        self.assertEqual(mock_stub.queryAnnotations.call_count, 1)

    def test_no_criteria_matches_all(self):
        mock_stub = Mock()
        mock_stub.queryAnnotations.return_value = self._page(["a"], "")
        self.client._stub = mock_stub

        self.assertEqual([a.name for a in self.client.iter_annotations()], ["a"])
        self.assertEqual(len(mock_stub.queryAnnotations.call_args_list[0].args[0].criteria), 0)

    def test_page_error_raises_runtime_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "page token rejected"
        mock_stub = Mock()
        mock_stub.queryAnnotations.return_value = response
        self.client._stub = mock_stub

        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_annotations(self.criteria))
        self.assertIn("page token rejected", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
