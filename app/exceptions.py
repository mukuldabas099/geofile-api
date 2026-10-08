"""Domain exceptions. Each carries the HTTP status and a stable machine-readable code."""


class GeoAPIError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, file_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.file_id = file_id


class UnsupportedFileTypeError(GeoAPIError):
    status_code = 415
    code = "unsupported_file_type"


class FileTooLargeError(GeoAPIError):
    status_code = 413
    code = "file_too_large"


class InvalidUploadError(GeoAPIError):
    status_code = 422
    code = "invalid_upload"


class InvalidArchiveError(GeoAPIError):
    status_code = 422
    code = "invalid_archive"


class UnreadableFileError(GeoAPIError):
    status_code = 422
    code = "unreadable_file"


class MissingCRSError(GeoAPIError):
    status_code = 422
    code = "missing_crs"


class EmptyFileError(GeoAPIError):
    status_code = 422
    code = "no_features"


class TooManyFeaturesError(GeoAPIError):
    status_code = 422
    code = "too_many_features"


class ResourceNotFoundError(GeoAPIError):
    status_code = 404
    code = "not_found"


class FileNotReadyError(GeoAPIError):
    status_code = 409
    code = "file_not_ready"
