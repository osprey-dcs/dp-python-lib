"""
Small helpers shared by the criteria-based query clients.

These live here rather than in any one feature client because more than one client needs them, and importing a
private name across feature modules (dataset_client importing from annotations_client, or the reverse) makes the
importing module's dependencies misleading and the imported name easy to break by accident.
"""

from typing import Any


def check_at_most_one_text_criterion(criteria: list[Any], op_name: str) -> None:
    """
    Rejects a criteria list carrying more than one text criterion.

    Two $text clauses cannot be ANDed -- Mongo rejects the query with "Too many text expressions" -- so the server
    rejects the second one during validation.  Catching it here fails with a message naming the rule instead of
    surfacing a server rejection.

    This is generic over the criterion types of the different query APIs: every one of them names its oneof
    "criterion" and its full-text arm "textCriterion", so the same check serves them all.

    :param criteria: The criteria list to check.
    :param op_name: The calling operation, used in the error message.
    :raises ValueError: if more than one criterion carries a textCriterion.
    """
    text_count = sum(1 for criterion in criteria if criterion.WhichOneof("criterion") == "textCriterion")
    if text_count > 1:
        raise ValueError(
            f"{op_name} accepts at most one text criterion, got {text_count}; two full-text clauses cannot be "
            f"combined with AND.  Combine the terms into a single text() criterion instead."
        )
