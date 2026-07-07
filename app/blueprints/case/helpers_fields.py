# app/blueprints/case/helpers_fields.py
from __future__ import annotations

from app.services.case.case_parameter_service import CaseParameterService

# Domestic
DOMESTIC_PATENT_FIELDS = CaseParameterService.get_field_layout("DOM", "PATENT")
DOMESTIC_PATENT_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("DOM", "PATENT")

DOMESTIC_DESIGN_FIELDS = CaseParameterService.get_field_layout("DOM", "DESIGN")
DOMESTIC_DESIGN_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("DOM", "DESIGN")

DOMESTIC_TRADEMARK_FIELDS = CaseParameterService.get_field_layout("DOM", "TRADEMARK")
DOMESTIC_TRADEMARK_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("DOM", "TRADEMARK")

# Incoming
INCOMING_PATENT_FIELDS = CaseParameterService.get_field_layout("INC", "PATENT")
INCOMING_PATENT_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("INC", "PATENT")

INCOMING_DESIGN_FIELDS = CaseParameterService.get_field_layout("INC", "DESIGN")
INCOMING_DESIGN_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("INC", "DESIGN")

INCOMING_TRADEMARK_FIELDS = CaseParameterService.get_field_layout("INC", "TRADEMARK")
INCOMING_TRADEMARK_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("INC", "TRADEMARK")

# Outgoing / PCT
OUTGOING_PATENT_FIELDS = CaseParameterService.get_field_layout("OUT", "PATENT")
OUTGOING_PATENT_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("OUT", "PATENT")

OUTGOING_DESIGN_FIELDS = CaseParameterService.get_field_layout("OUT", "DESIGN")
OUTGOING_DESIGN_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("OUT", "DESIGN")

OUTGOING_TRADEMARK_FIELDS = CaseParameterService.get_field_layout("OUT", "TRADEMARK")
OUTGOING_TRADEMARK_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("OUT", "TRADEMARK")

PCT_FIELDS = CaseParameterService.get_field_layout("OUT", "PCT")
PCT_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("OUT", "PCT")

# Litigation
LITIGATION_FIELDS = CaseParameterService.get_field_layout("IP", "LITIGATION")
LITIGATION_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("IP", "LITIGATION")

# Misc
MISC_FIELDS = CaseParameterService.get_field_layout("IP", "MISC")
MISC_ALLOWED_KEYS = CaseParameterService.get_allowed_keys("IP", "MISC")
