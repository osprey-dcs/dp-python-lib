from abc import ABC


class ResultStatus():
    """
    Encapsulates status information for a call to a service API method including flag indicating success or failure
    of the operation, and a corresponding error message.
    """

    def __init__(self, is_error, message=""):
        """
        :param is_error: Boolean flag indicating success or failure of the API method call.
        :param message: Corresponding error message. Defaults to empty string when method is successful.
        """
        self.is_error = is_error
        self.message = message

class ApiResultBase(ABC):
    """
    Abstract base class for returning information from service API method calls.  Concrete subclasses contain
    details from the API method response, in addition to status information.
    """

    def __init__(self, is_error, message):
        """
        :param is_error: Boolean flag indicating success or failure of the API method call.
        :param message: Corresponding error message. Defaults to empty string when method is successful.
        """
        self.result_status = ResultStatus(is_error, message)
