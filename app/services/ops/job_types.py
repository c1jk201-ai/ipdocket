from enum import Enum


class JobType(str, Enum):
    INLINE = "inline"
    AFTER_COMMIT = "after_commit"
    SCHEDULER = "scheduler"
