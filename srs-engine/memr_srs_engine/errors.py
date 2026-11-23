"""Public errors never include backend diagnostics or collection content."""
from anki import errors as anki_errors


class EngineError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_dict(self):
        result = {"code": self.code, "message": self.message}
        if self.data is not None:
            result["data"] = self.data
        return result


def public_error(error):
    if isinstance(error, EngineError):
        return error
    if isinstance(error, (anki_errors.Interrupted, anki_errors.AbortSchemaModification)):
        return EngineError("aborted", "The operation was aborted.")
    if isinstance(error, (anki_errors.NotFoundError, anki_errors.DeletedError, anki_errors.UndoEmpty)):
        return EngineError("not_found", "The requested item is unavailable.")
    if isinstance(error, (anki_errors.InvalidInput, anki_errors.SearchError,
                          anki_errors.TemplateError, anki_errors.CardTypeError,
                          anki_errors.CustomStudyError, anki_errors.FilteredDeckError)):
        return EngineError("invalid_params", "The engine rejected the input.")
    if isinstance(error, anki_errors.ExistsError):
        return EngineError("conflict", "The requested item already exists.")
    if isinstance(error, anki_errors.SchedulerUpgradeRequired):
        return EngineError("unsupported", "The collection requires a scheduler upgrade.")
    if isinstance(error, (anki_errors.NetworkError, anki_errors.SyncError)):
        return EngineError("engine_unavailable", "The sync service is unavailable.")
    if isinstance(error, anki_errors.DBError) and any(
        text in str(error).lower() for text in ("already open", "database is locked", "media currently syncing")
    ):
        return EngineError("conflict", "collection is open in another program")
    return EngineError("internal", "The engine could not complete the request.")
