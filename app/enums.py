from enum import Enum


class FileType(str, Enum):
    KML = "kml"
    SHAPEFILE = "shapefile"


class FileStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MeasurementStatus(str, Enum):
    OK = "OK"  # area / length computed
    NOT_APPLICABLE = "NOT_APPLICABLE"  # e.g. points: nothing to measure by design
    UNSUPPORTED = "UNSUPPORTED"  # geometry type (or empty geometry) we cannot measure
    FAILED = "FAILED"  # measurement attempted but errored (reported, never raised)
