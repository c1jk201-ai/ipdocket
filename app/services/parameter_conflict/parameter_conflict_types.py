from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ConflictItem:
    field_name: str
    field_label: str
    current_value: Optional[Any]
    new_value: Optional[Any]
    table_name: str
    field_key: str
    priority: int = 1
    hidden: bool = False

    @property
    def has_conflict(self) -> bool:
        if not self.current_value or not self.new_value:
            return False
        return self._normalize(self.current_value) != self._normalize(self.new_value)

    def _normalize(self, val: Any) -> str:
        import re

        s = str(val)
        name_lower = self.field_name.lower()
        is_id = False

        if name_lower.startswith("identifier_"):
            is_id = True
        elif any(k in name_lower for k in ("_no", "_date", "_count", "_id", "code")):
            is_id = True
        elif name_lower in ("app_no", "application_no", "reg_no", "pub_no", "customer_no"):
            is_id = True

        if is_id:
            return re.sub(r"[^a-zA-Z0-9]", "", s).upper()

        return re.sub(r"\s+", " ", s).strip()


def _parse_date_str(d_str: str):
    import re
    from datetime import date

    if not d_str:
        return None
    nums = re.findall(r"\d+", str(d_str))
    if len(nums) >= 3:
        try:
            return date(int(nums[0]), int(nums[1]), int(nums[2]))
        except (ValueError, TypeError):
            return None
    return None


def _normalize_identifier(val: Any) -> str:
    import re

    s = str(val or "")
    return re.sub(r"[^a-zA-Z0-9]", "", s).upper()


@dataclass
class ParameterExtractionResult:
    matter_id: str
    our_ref: str
    doc_type: str
    auto_apply: list[ConflictItem]
    conflicts: list[ConflictItem]
    skipped: list[str]
