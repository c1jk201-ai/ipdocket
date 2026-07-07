from __future__ import annotations

import json
from typing import Any, Optional

try:
    from openai import OpenAI, OpenAIError
except ImportError:
    OpenAI = None
    OpenAIError = Exception

from app.services.core.llm_model_registry import resolve_llm_model
from app.services.core.llm_runtime import get_openai_api_key

NOTICE_DUE_POLICY_SYSTEM_PROMPT = """
 Patent/Trademark/Design Matter Deadline(docketing) Contact.

Inputto 'Send(Document name)' . below :

- due_date_confirmation_required = true:
  Document  //Payment//Deadline/  "Statutory     Deadline" ,
  Confirm whether the document name indicates a deadline.

- due_date_confirmation_required = false:
  Client   , to  /Payment/  Deadline Confirm Required  Guidance/Notice//  .

:
-  true . (to 'Confirm Required')
-     JSON.
"""


NOTICE_DUE_POLICY_JSON_SCHEMA = {
    "name": "NoticeDuePolicySuggestion",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["due_date_confirmation_required", "confidence", "reason"],
        "properties": {
            "due_date_confirmation_required": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
            "reason": {"type": "string"},
        },
    },
    "strict": True,
}


def suggest_notice_due_policy_from_title(
    *,
    doc_name: str,
    division: str | None = None,
    doc_code: str | None = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict[str, Any]:
    """
    LLM-based suggestion for whether due date confirmation is required.

    Intended as a *last-resort* helper:
    - DOM/INC: maintain deterministic policies; use this only for unseen/ambiguous doc names.
    - OUT: may rely on LLM more (foreign notices are hard to hardcode).
    """
    if OpenAI is None:
        raise RuntimeError("OpenAI package is not installed.")

    clean_name = (doc_name or "").strip()
    if not clean_name:
        raise ValueError("doc_name is required")

    api_key = (api_key or "").strip() or get_openai_api_key(allow_legacy=False)
    if not api_key:
        raise ValueError("OpenAI API key is required (OPENAI_API_KEY)")

    model = (model or "").strip() or resolve_llm_model("notice_due_policy")

    div = (division or "").strip().upper() or "UNKNOWN"
    dc = (doc_code or "").strip()

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": NOTICE_DUE_POLICY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "\n".join(
                        [
                            f"division={div}",
                            f"doc_code={dc or '-'}",
                            f"doc_name={clean_name}",
                        ]
                    ),
                },
            ],
            response_format={"type": "json_schema", "json_schema": NOTICE_DUE_POLICY_JSON_SCHEMA},
            temperature=0,
        )
        return json.loads(response.choices[0].message.content)
    except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RuntimeError(f"LLM due-policy suggestion failed: {exc}") from exc
