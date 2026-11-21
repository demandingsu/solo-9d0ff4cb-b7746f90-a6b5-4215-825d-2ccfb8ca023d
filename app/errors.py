"""Domain exceptions used across the reconciliation service."""


class ReconciliationError(Exception):
    """Base class for domain-level failures that should be shown to users."""

    code = "reconciliation_error"


class CsvParseError(ReconciliationError):
    code = "csv_parse_error"


class MappingValidationError(ReconciliationError):
    """The uploaded tree mapping is not a valid forest for reconciliation."""

    code = "mapping_validation_error"


class SnapshotValidationError(ReconciliationError):
    """A submission is internally inconsistent or incomplete."""

    code = "snapshot_validation_error"


class ActualsValidationError(ReconciliationError):
    """A batch of actual production figures violates the reporting rules.

    ``details`` carries machine-readable context (for example the conflicting
    shops) so the API can return it and the page can render it.
    """

    code = "actuals_validation_error"

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class OptimizationError(ReconciliationError):
    """The weighted least-squares correction could not be solved."""

    code = "optimization_error"


class StalePreviewError(ReconciliationError):
    """A preview belongs to a superseded input version and may not be published."""

    code = "stale_preview_error"


class PublishError(ReconciliationError):
    code = "publish_error"
