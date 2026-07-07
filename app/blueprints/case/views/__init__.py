# Case Views Package
#
# This package contains the refactored case history and file routes:
# - history_letter: Letter CRUD operations
# - history_notice: Notice (Office Action) CRUD operations
# - file_assets: File download, EML viewing, attachments
# - file_manager: FM upload, folder, move, delete
# - _common: Shared utilities

from . import (
    extracted_params,
    file_assets,
    file_manager,
    history_letter,
    history_notice,
)
