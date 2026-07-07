from __future__ import annotations

FOREIGN_EMAIL_IDENTIFIERS_SYSTEM_PROMPT = (
    "Extract foreign IP matter identifiers from the supplied email or document text. "
    "Return only JSON matching the schema."
)

FOREIGN_EMAIL_IDENTIFIERS_SCHEMA = {
    "name": "foreign_email_identifiers",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "identifiers": {
                "type": "object",
                "properties": {
                    "our_ref": {"type": ["string", "null"]},
                    "application_no": {"type": ["string", "null"]},
                    "publication_no": {"type": ["string", "null"]},
                    "registration_no": {"type": ["string", "null"]},
                    "pct_no": {"type": ["string", "null"]},
                    "agent_ref": {"type": ["string", "null"]},
                    "client_ref": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                },
                "additionalProperties": True,
            }
        },
        "required": ["identifiers"],
        "additionalProperties": True,
    },
}

FOREIGN_EMAIL_EXTRACT_SYSTEM_PROMPT = (
    "Extract filing parameters, deadlines, and evidence from a foreign IP email. "
    "Return JSON only and cite evidence_map entries for extracted dates."
)

FOREIGN_EMAIL_EXTRACT_V1_SCHEMA = {
    "name": "foreign_email_extract_v1",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "schema": {"type": "string"},
            "case_target": {"type": "object", "additionalProperties": True},
            "params": {"type": "object", "additionalProperties": True},
            "dockets": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "evidence_map": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_path": {"type": "string"},
                        "attachment_sha256": {"type": ["string", "null"]},
                        "page": {"type": ["integer", "null"]},
                        "snippet": {"type": "string"},
                        "char_start": {"type": "integer"},
                        "char_end": {"type": "integer"},
                    },
                    "required": ["field_path", "snippet", "char_start", "char_end"],
                    "additionalProperties": True,
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["schema", "case_target", "params", "dockets", "evidence_map"],
        "additionalProperties": True,
    },
}
