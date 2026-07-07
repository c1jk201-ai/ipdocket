from __future__ import annotations

"""LLM parsing helpers for billing and client data."""

import base64
import io
import logging

from flask import current_app

from app.services.core.llm_model_registry import resolve_llm_model

try:
  from openai import OpenAI
except ImportError:
  OpenAI = None

try:
  import openai as _openai

  OpenAIError = getattr(_openai, "OpenAIError", Exception)
except ImportError:
  OpenAIError = Exception

SYSTEM_PROMPT = """\
You are a data extraction assistant for a U.S. IP docketing and billing system.
Given the raw body of an email, extract client data under these rules.
Do not treat internal firm email addresses at @example.com as client contact data.

Language rules:
- Determine the email's primary language from the body.
- Preserve proper names, company names, brands, and addresses exactly as written.
- Write notes naturally in the primary language, but default to English when uncertain.

Notes prefix:
- Start notes with one of: "Category: Foreign agent", "Category: Company", or "Category: Individual".

Schema rules:
1) Only return {name,email,phone,address,manager,notes}; missing values are empty strings.
2) name: use the official firm/company/institution name, or the person's full name for individuals.
3) manager: the actual sender/contact person's name only, excluding department or title when possible.
4) email: the best primary contact email.
5) phone: the most complete phone number, preferably with country code.
6) address: the primary mailing address, preserving source formatting.
7) notes: concise uncertainty or supplemental information.

Return JSON only. Do not include markdown or explanatory text.
"""

JSON_SCHEMA = {
  "name": "Customer",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "email", "phone", "address", "manager", "notes"],
    "properties": {
      "name": {"type": "string"},
      "email": {"type": "string"},
      "phone": {"type": "string"},
      "address": {"type": "string"},
      "manager": {"type": "string"},
      "notes": {"type": "string"},
    },
  },
  "strict": True,
}

_LOGGER = logging.getLogger(__name__)


def _get_logger():
  try:
    return current_app.logger
  except Exception:
    return _LOGGER


def _billing_llm_model() -> str:
  return resolve_llm_model("billing_invoice")


def parse_customer_from_text(email_text: str, api_key: str) -> dict:
  """
  Extract client information from email text using the OpenAI API.

  Args:
    email_text: Email body text.
    api_key: OpenAI API key.

  Returns:
    dict: Parsed client information {name, email, phone, address, manager, notes}.

  Raises:
    Exception: Raised when the API call fails.
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")

  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  client = OpenAI(api_key=api_key)

  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(), # Structured Outputs model.
      messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Extract client information from the following email content:\n\n{email_text}",
        },
      ],
      response_format={"type": "json_schema", "json_schema": JSON_SCHEMA},
      temperature=0,
    )

    import json

    result = json.loads(response.choices[0].message.content)

    # Return empty strings as empty strings instead of converting them to None.
    # (All fields already return strings.)
    return result

  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM parsing failed: {str(e)}")


# ===================== Payment record parser ===================== #
PAYMENT_SYSTEM_PROMPT = """
You are a payment-record extraction assistant for a U.S. IP docketing and billing system.
Extract the fields below from bank activity, receipt, ACH, wire, or deposit text.

Rules:
- Return currency as a three-letter ISO 4217 code. Use USD when the document does not specify a currency.
- deposit must be a numeric string without commas, for example "1998.60".
- date must be YYYY-MM-DD when possible.
- time must be HH:MM:SS when available; otherwise return an empty string.
- Missing values must be empty strings.

Fields:
- account_alias: account nickname or bank account label.
- date: transaction date.
- time: transaction time.
- deposit: payment/deposit amount.
- currency: ISO currency code, normally USD.
- summary: payer, counterparty, or transaction description.
- channel: payment rail or source, such as ACH, wire, check, card, or bank portal.
- cms_code: external transaction or processor code when present.
"""

PAYMENT_JSON_SCHEMA = {
  "name": "USDPaymentMeta",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": [
      "account_alias",
      "date",
      "time",
      "deposit",
      "currency",
      "summary",
      "channel",
      "cms_code",
    ],
    "properties": {
      "account_alias": {"type": "string"},
      "date": {"type": "string"},
      "time": {"type": "string"},
      "deposit": {"type": "string"},
      "currency": {"type": "string"},
      "summary": {"type": "string"},
      "channel": {"type": "string"},
      "cms_code": {"type": "string"},
    },
  },
  "strict": True,
}


def parse_usd_payment_rule_based(text: str) -> dict | None:
  """
  Parse payment information with simple rule-based extraction.

  Example input format:
  3
  column
  row (****-6033)
  2025-06-16
  10:28:30
  1,998,600
  0
  40,483,357
  USD
  （）
   ()
  -

  Returns:
    dict | None: Payment metadata on success, otherwise None.
  """
  import re

  # Normalize:
  # - Replace non-breaking spaces with normal spaces
  # - Convert tabs to newlines so each column becomes its own line
  # - Convert runs of 2+ spaces to newlines to split columnar text
  normalized = text.replace("\u00a0", " ")
  normalized = normalized.replace("\t", "\n")
  normalized = re.sub(r"[ \u00A0]{2,}", "\n", normalized)
  lines = [line.strip() for line in normalized.strip().split("\n") if line.strip()]

  # Require enough fields; time and external code are optional.
  if len(lines) < 8:
    return None

  try:
    # Extract fields in order.
    idx = 0

    # Skip the first line when it is just an index number.
    if lines[idx].isdigit():
      idx += 1

    # Bank account alias.
    account_alias = lines[idx] if idx < len(lines) else ""
    idx += 1

    # Skip bank details when the row appears to contain masked account data.
    if idx < len(lines) and ("(" in lines[idx] or "****" in lines[idx]):
      idx += 1

    # Transaction date (YYYY-MM-DD or YYYY/MM/DD).
    date_str = lines[idx] if idx < len(lines) else ""
    date_str = date_str.replace("/", "-") # Convert slashes to hyphens.
    if not re.match(r"\d{4}-\d{2}-\d{2}", date_str):
      return None # Fail when the date format is invalid.
    idx += 1

    # Transaction time (HH:MM:SS or HH:MM), optional.
    time_str = ""
    if idx < len(lines):
      maybe_time = lines[idx]
      if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", maybe_time):
        parts = maybe_time.split(":")
        time_str = f"{parts[0]}:{parts[1]}:{parts[2] if len(parts) == 3 else '00'}"
        idx += 1

    # Deposit amount, possibly with commas.
    deposit_str = lines[idx] if idx < len(lines) else ""
    deposit_clean = deposit_str.replace(",", "").replace(" ", "")
    if not deposit_clean.isdigit():
      return None # Fail when the amount is not numeric.
    idx += 1

    # Skip the next two optional amount/balance fields.
    idx += 2

    # Currency
    currency = lines[idx] if idx < len(lines) else "USD"
    idx += 1

    # Summary or counterparty.
    summary = lines[idx] if idx < len(lines) else ""
    idx += 1

    # Channel, optional.
    channel = lines[idx] if idx < len(lines) else ""
    if channel == "-":
      channel = ""
    idx += 1

    # External transaction code, optional.
    cms_code = lines[idx] if idx < len(lines) else ""
    if cms_code == "-":
      cms_code = ""

    # Validate required fields; time is optional.
    if not all([account_alias, date_str, deposit_clean, summary]):
      return None

    return {
      "account_alias": account_alias,
      "date": date_str,
      "time": time_str,
      "deposit": deposit_clean,
      "currency": currency.upper() if currency else "USD",
      "summary": summary,
      "channel": channel,
      "cms_code": cms_code,
    }

  except (IndexError, ValueError, TypeError, AttributeError):
    return None


def parse_usd_payment_from_text(text: str, api_key: str) -> tuple[dict, str]:
  """
  Parse payment information using rules first, then LLM fallback.

  First pass: rule-based parsing.
  Second pass: OpenAI LLM fallback.

  Args:
    text: Input text, such as OCR output.
    api_key: OpenAI API key.

  Returns:
    tuple[dict, str]: (Payment , parser type: "rule" "llm")
  """
  # Step 1: rule-based parsing.
  rule_result = parse_usd_payment_rule_based(text)
  if rule_result:
    _get_logger().debug("payment parser: rule-based success")
    return rule_result, "rule"

  # Step 2: LLM fallback.
  _get_logger().info("payment parser: rule-based failed; falling back to LLM")
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": PAYMENT_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Extract payment information from the following transaction text:\n\n{text}",
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": PAYMENT_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json

    result = json.loads(response.choices[0].message.content)
    return result, "llm"
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM payment parsing failed: {str(e)}")


# ===================== Foreign-currency payment parser ===================== #
FX_PAYMENT_SYSTEM_PROMPT = """
You are a foreign-currency payment extraction assistant for a U.S. IP docketing and billing system.
Extract the deposit amount and transaction date from bank, wire, receipt, or payment text.

Rules:
- deposit must be a numeric string without commas, for example "875.00".
- date must be YYYY-MM-DD or YYYY-MM-DD HH:MM when possible.
- Missing values must be empty strings.
"""

FX_PAYMENT_JSON_SCHEMA = {
  "name": "FXPaymentMeta",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": ["deposit", "date"],
    "properties": {
      "deposit": {"type": "string"},
      "date": {"type": "string"},
    },
  },
  "strict": True,
}


def parse_fx_payment_from_text(text: str, api_key: str) -> tuple[dict, str]:
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": FX_PAYMENT_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Extract payment information from the following transaction text:\n\n{text}",
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": FX_PAYMENT_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json

    result = json.loads(response.choices[0].message.content)
    return result, "llm"
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM FX payment parsing failed: {str(e)}")


# ===================== Unified payment parser ===================== #
def parse_payment_from_text(text: str, currency: str, api_key: str) -> tuple[dict, str]:
  """
  Route to the appropriate payment parser based on currency.
  """
  if currency and str(currency).upper() in {"USD", "USD"}:
    return parse_usd_payment_from_text(text, api_key)
  return parse_fx_payment_from_text(text, api_key)


# ===================== Generic document summarizer (PDF 1st page) ===================== #
DOC_SUMMARY_SYSTEM_PROMPT = """
You are a document summarization assistant for a U.S. IP docketing and billing system.
Read the first-page OCR/text and populate the schema.

Guidance:
- For payment, remittance, receipt, invoice, or bank documents, fill sender, receiver, amount, currency, date, and reference when available.
- For other documents, infer doc_type and fill only fields supported by the source.
- summary must be concise English in 2 to 4 sentences.
- Missing or uncertain values must be empty strings.
"""

DOC_SUMMARY_JSON_SCHEMA = {
  "name": "DocSummary",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": [
      "summary",
      "doc_type",
      "sender",
      "receiver",
      "amount",
      "currency",
      "date",
      "reference",
    ],
    "properties": {
      "summary": {"type": "string"},
      "doc_type": {"type": "string"},
      "sender": {"type": "string"},
      "receiver": {"type": "string"},
      "amount": {"type": "string"},
      "currency": {"type": "string"},
      "date": {"type": "string"},
      "reference": {"type": "string"},
    },
  },
  "strict": True,
}


def summarize_document_from_text(text: str, api_key: str) -> dict:
  """
  Summarize and structure document text, such as first-page PDF extraction.

  Returns: dict with keys from DOC_SUMMARY_JSON_SCHEMA
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")

  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": DOC_SUMMARY_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Summarize and structure the following first-page document content:\n\n{text}",
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": DOC_SUMMARY_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return result
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM summarization failed: {str(e)}")


def summarize_document_from_image(img, api_key: str, max_side: int = 1600) -> dict:
  """
  Summarize and structure a document image with a vision model.

  Returns: dict with keys from DOC_SUMMARY_JSON_SCHEMA
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")

  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  try:
    from PIL import Image

    if not isinstance(img, Image.Image):
      raise ValueError("img must be a PIL.Image")
  except (ImportError, ValueError, TypeError):
    raise

  # Convert to RGB and downscale
  try:
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGB")
  except (AttributeError, ValueError, TypeError, OSError):
    pass
  try:
    w, h = img.size
    m = max(w, h)
    if max_side and m > max_side:
      scale = max_side / float(m)
      img = img.resize((int(w * scale), int(h * scale)))
  except (AttributeError, ValueError, TypeError, OSError):
    pass

  # Encode to PNG base64
  buf = io.BytesIO()
  img.save(buf, format="PNG")
  b64 = base64.b64encode(buf.getvalue()).decode("ascii")
  data_url = f"data:image/png;base64,{b64}"

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": DOC_SUMMARY_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": [
            {
              "type": "text",
              "text": "Summarize and structure the key information from the document image below.",
            },
            {"type": "image_url", "image_url": {"url": data_url}},
          ],
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": DOC_SUMMARY_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return result
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM vision summarization failed: {str(e)}")


# ===================== Foreign remittance proof parser ===================== #
FOREIGN_REMITTANCE_SYSTEM_PROMPT = """
You are a foreign remittance proof extraction assistant for a U.S. IP docketing and billing system.
Extract structured fields from wire confirmations, remittance receipts, bank records, or payment confirmations.

Rules:
- If the document is a wire transfer confirmation, remittance receipt, or payment confirmation, set doc_type to "foreign_remittance_proof".
- amount should be the foreign-currency remitted amount when present.
- currency must be a three-letter ISO 4217 code, such as USD, JPY, or EUR.
- date must be the actual transfer, transaction, or processing date in YYYY-MM-DD format.
- usd_amount should be populated only when the document shows a USD debit or equivalent amount.
- Preserve displayed account numbers, including masking characters.
- Missing values must be empty strings.
- summary must be one English sentence focused on transfer date, amount, and recipient.
"""

FOREIGN_REMITTANCE_JSON_SCHEMA = {
  "name": "ForeignRemittanceProof",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": [
      "summary",
      "doc_type",
      "sender",
      "receiver",
      "amount",
      "currency",
      "date",
      "reference",
      "sender_bank",
      "sender_account",
      "receiver_bank",
      "receiver_account",
      "swift_code",
      "usd_amount",
      "exchange_rate",
      "fee_amount",
      "purpose",
      "confidence",
    ],
    "properties": {
      "summary": {"type": "string"},
      "doc_type": {"type": "string"},
      "sender": {"type": "string"},
      "receiver": {"type": "string"},
      "amount": {"type": "string"},
      "currency": {"type": "string"},
      "date": {"type": "string"},
      "reference": {"type": "string"},
      "sender_bank": {"type": "string"},
      "sender_account": {"type": "string"},
      "receiver_bank": {"type": "string"},
      "receiver_account": {"type": "string"},
      "swift_code": {"type": "string"},
      "usd_amount": {"type": "string"},
      "exchange_rate": {"type": "string"},
      "fee_amount": {"type": "string"},
      "purpose": {"type": "string"},
      "confidence": {"type": "string"},
    },
  },
  "strict": True,
}

_REMITTANCE_FIELDS = (
  "summary",
  "doc_type",
  "sender",
  "receiver",
  "amount",
  "currency",
  "date",
  "reference",
  "sender_bank",
  "sender_account",
  "receiver_bank",
  "receiver_account",
  "swift_code",
  "usd_amount",
  "exchange_rate",
  "fee_amount",
  "purpose",
  "confidence",
)

_REMITTANCE_CURRENCIES = (
  "USD",
  "JPY",
  "EUR",
  "CNY",
  "GBP",
  "AUD",
  "CAD",
  "CHF",
  "HKD",
  "SGD",
  "TWD",
  "USD",
)


def _empty_foreign_remittance_result() -> dict:
  return {key: "" for key in _REMITTANCE_FIELDS}


def _normalize_remittance_date(value: str) -> str:
  if not value:
    return ""
  import re

  text = str(value).strip()
  text = re.sub(r"[./]", "-", text)
  m = re.search(r"(\d{4})[- ]?(\d{1,2})[- ]?(\d{1,2})", text)
  if not m:
    return ""
  try:
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
  except ValueError:
    return ""


def _normalize_money_value(value: str) -> str:
  if not value:
    return ""
  import re

  text = str(value).strip()
  text = re.sub(r"(?i)\b(USD|JPY|EUR|CNY|GBP|AUD|CAD|CHF|HKD|SGD|TWD|USD)\b", "", text)
  text = text.replace(",", "").replace(" ", "")
  m = re.search(r"-?\d+(?:\.\d+)?", text)
  return m.group(0) if m else ""


def _normalize_currency(value: str) -> str:
  if not value:
    return ""
  import re

  text = str(value).upper().strip()
  aliases = {
    "US$": "USD",
    "$": "USD",
    "￥": "JPY",
    "¥": "JPY",
    "EURO": "EUR",
    "": "JPY",
    "": "USD",
    "USD": "USD",
  }
  if text in aliases:
    return aliases[text]
  m = re.search(r"\b(USD|JPY|EUR|CNY|GBP|AUD|CAD|CHF|HKD|SGD|TWD|USD)\b", text)
  if m:
    return m.group(1)
  return ""


def _remittance_lines(text: str) -> list[str]:
  import re

  normalized = (text or "").replace("\r", "\n").replace("\u00a0", " ")
  normalized = normalized.replace("\t", "\n")
  normalized = re.sub(r"[ \u3000]{2,}", "\n", normalized)
  return [line.strip(" \t:：") for line in normalized.split("\n") if line.strip()]


def _clean_remittance_value(value: str) -> str:
  import re

  text = str(value or "").strip(" \t:：-")
  text = re.sub(r"\s{2,}", " ", text)
  return text[:160].strip()


def _plain_remittance_lines(lines: list[str]) -> list[str]:
  import re

  plain: list[str] = []
  for line in lines:
    value = _clean_remittance_value(line)
    value = re.sub(r"^(?:text|field|label|value)\s+", "", value, flags=re.IGNORECASE)
    if value:
      plain.append(value)
  return plain


def _looks_like_remittance_bank(line: str) -> bool:
  upper = (line or "").upper()
  return any(term in upper for term in ("BANK", "CHASE", "CITI", "WELLS FARGO", "BOFA"))


def _looks_like_remittance_org(line: str) -> bool:
  import re

  if not line or _looks_like_remittance_bank(line):
    return False
  if re.search(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", line):
    return False
  if re.fullmatch(r"[0-9 ,.$]+", line):
    return False
  legal_terms = (
    "LAW",
    "FIRM",
    "LLP",
    "LLC",
    "INC",
    "CORP",
    "COMPANY",
    "OFFICE",
    "PATENT",
    "IP",
    "ATTORNEY",
  )
  upper = line.upper()
  return any(term in upper for term in legal_terms)


def _looks_like_remittance_account(line: str) -> bool:
  import re

  text = (line or "").replace(" ", "").replace("-", "")
  return bool(re.fullmatch(r"\d{6,34}", text))


def _value_after_label(lines: list[str], label_patterns: list[str]) -> str:
  import re

  for idx, line in enumerate(lines):
    for pattern in label_patterns:
      if not pattern.strip() or re.fullmatch(pattern, "", re.IGNORECASE):
        continue
      m = re.search(pattern, line, re.IGNORECASE)
      if not m:
        continue
      value = _clean_remittance_value(line[m.end() :])
      if value:
        return value
      for next_line in lines[idx + 1 : idx + 4]:
        candidate = _clean_remittance_value(next_line)
        if candidate:
          return candidate
  return ""


def _extract_currency_amount(value: str) -> tuple[str, str]:
  if not value:
    return "", ""
  import re

  text = str(value)
  currency_pattern = "|".join(_REMITTANCE_CURRENCIES)
  patterns = (
    rf"(?i)\b({currency_pattern})\b\s*([0-9][0-9,]*(?:\.\d+)?)",
    rf"(?i)([0-9][0-9,]*(?:\.\d+)?)\s*\b({currency_pattern})\b",
    r"([¥￥$])\s*([0-9][0-9,]*(?:\.\d+)?)",
  )
  for pattern in patterns:
    m = re.search(pattern, text)
    if not m:
      continue
    if len(m.group(1)) == 1 and m.group(1) in {"¥", "￥", "$"}:
      return _normalize_currency(m.group(1)), _normalize_money_value(m.group(2))
    if _normalize_currency(m.group(1)):
      return _normalize_currency(m.group(1)), _normalize_money_value(m.group(2))
    return _normalize_currency(m.group(2)), _normalize_money_value(m.group(1))
  amount = _normalize_money_value(text)
  currency = _normalize_currency(text)
  return currency, amount


def _normalize_foreign_remittance_result(result: dict, parser: str | None = None) -> dict:
  normalized = _empty_foreign_remittance_result()
  if isinstance(result, dict):
    for key in _REMITTANCE_FIELDS:
      value = result.get(key)
      normalized[key] = "" if value is None else str(value).strip()

  normalized["currency"] = _normalize_currency(normalized.get("currency") or "")
  normalized["date"] = _normalize_remittance_date(normalized.get("date") or "")
  for key in ("amount", "usd_amount", "exchange_rate", "fee_amount"):
    normalized[key] = _normalize_money_value(normalized.get(key) or "")

  doc_type = (normalized.get("doc_type") or "").strip().lower()
  if doc_type in {"remittance", "wire_transfer", "wire transfer", "Remittance proof", "Confirm", ""}:
    normalized["doc_type"] = (
      "foreign_remittance_proof" if _foreign_remittance_core_score(normalized) else doc_type
    )

  if not normalized.get("summary"):
    parts = []
    if normalized.get("date"):
      parts.append(normalized["date"])
    if normalized.get("currency") and normalized.get("amount"):
      parts.append(f"{normalized['currency']} {normalized['amount']}")
    if normalized.get("receiver"):
      parts.append(f" {normalized['receiver']}")
    normalized["summary"] = " / ".join(parts)

  if parser:
    normalized["parser"] = parser
  return normalized


def _foreign_remittance_core_score(result: dict) -> int:
  return sum(
    1 for key in ("amount", "currency", "date", "receiver", "reference") if result.get(key)
  )


def parse_foreign_remittance_proof_rule_based(text: str) -> dict:
  import re

  result = _empty_foreign_remittance_result()
  if not text:
    return _normalize_foreign_remittance_result(result, parser="rule")

  lines = _remittance_lines(text)
  plain_lines = _plain_remittance_lines(lines)
  compact = "\n".join(lines)

  remittance_keywords = (
    "Foreign",
    "Foreign currency",
    "Confirm",
    "",
    "Remittance proof",
    "remittance",
    "wire transfer",
    "telegraphic transfer",
  )
  if any(keyword.lower() in compact.lower() for keyword in remittance_keywords):
    result["doc_type"] = "foreign_remittance_proof"

  result["sender"] = _value_after_label(
    lines,
    [
      r"\s*",
      r"\s*(|)",
      r"\s*",
      r"remitter",
      r"applicant",
      r"ordering\s+customer",
      r"sender",
    ],
  )
  result["receiver"] = _value_after_label(
    lines,
    [
      r"\s*",
      r"\s*(|)",
      r"\s*",
      r"beneficiary",
      r"receiver",
      r"payee",
    ],
  )
  result["sender_bank"] = _value_after_label(
    lines,
    [r"\s*row", r"\s*row", r"remitting\s+bank", r"sender\s+bank"],
  )
  result["receiver_bank"] = _value_after_label(
    lines,
    [
      r"\s*row",
      r"\s*row",
      r"beneficiary\s+bank",
      r"receiving\s+bank",
      r"receiver\s+bank",
    ],
  )
  result["sender_account"] = _value_after_label(
    lines,
    [r"\s*Bank account", r"\s*Bank account", r"sender\s+account", r"debit\s+account"],
  )
  result["receiver_account"] = _value_after_label(
    lines,
    [
      r"\s*Bank account",
      r"Deposit\s*Bank account",
      r"beneficiary\s+account",
      r"receiver\s+account",
      r"account\s+no",
    ],
  )
  result["purpose"] = _value_after_label(
    lines, [r"\s*", r"\s*", r"purpose", r"payment\s+details"]
  )

  org_candidates = [line for line in plain_lines if _looks_like_remittance_org(line)]
  if not result["sender"] and org_candidates:
    result["sender"] = org_candidates[0]
  if not result["receiver"] and org_candidates:
    result["receiver"] = org_candidates[1] if len(org_candidates) > 1 else org_candidates[0]

  if not result["receiver_bank"]:
    for line in plain_lines:
      if _looks_like_remittance_bank(line):
        result["receiver_bank"] = line
        break

  if not result["receiver_account"]:
    bank_index = -1
    if result["receiver_bank"]:
      try:
        bank_index = plain_lines.index(result["receiver_bank"])
      except ValueError:
        bank_index = -1
    account_candidates = [
      line for line in plain_lines[max(bank_index + 1, 0) :] if _looks_like_remittance_account(line)
    ]
    if account_candidates:
      result["receiver_account"] = account_candidates[0].replace(" ", "").replace("-", "")

  date_value = _value_after_label(
    lines,
    [
      r"processing\s+date",
      r"process\s+date",
      r"value\s+date",
      r"transaction\s+date",
      r"value\s+date",
      r"transaction\s+date",
      r"remittance\s+date",
      r"date",
    ],
  )
  result["date"] = _normalize_remittance_date(date_value)
  if not result["date"]:
    m = re.search(r"(\d{4}[./-]\s*\d{1,2}[./-]\s*\d{1,2})", compact)
    if m:
      result["date"] = _normalize_remittance_date(m.group(1))

  amount_value = _value_after_label(
    lines,
    [
      r"Foreign currency\s*\s*",
      r"\s*Amount",
      r"\s*Amount",
      r"remittance\s+amount",
      r"transfer\s+amount",
      r"amount",
    ],
  )
  currency, amount = _extract_currency_amount(amount_value)
  if not amount:
    currency, amount = _extract_currency_amount(compact)
  result["currency"] = currency
  result["amount"] = amount

  usd_value = _value_after_label(
    lines,
    [r"USD\s*(Amount|Amount|)", r"\s*Amount", r"usd\s+amount", r"debit\s+amount"],
  )
  result["usd_amount"] = _normalize_money_value(usd_value)
  result["exchange_rate"] = _normalize_money_value(
    _value_after_label(lines, [r"Apply\s*Exchange rate", r"Exchange rate", r"exchange\s+rate"])
  )
  result["fee_amount"] = _normalize_money_value(
    _value_after_label(lines, [r"Fee", r"fee", r"charge"])
  )

  result["reference"] = _value_after_label(
    lines,
    [
      r"\s*",
      r"\s*",
      r"\s*",
      r"\s*",
      r"reference\s*(no\.?|number)?",
      r"transaction\s*(no\.?|number)?",
      r"uetr",
    ],
  )
  if not result["reference"]:
    m = re.search(r"\b[A-Z]{2,12}-\d{6,}(?:-\d+)?\b", compact)
    if m:
      result["reference"] = m.group(0)
  swift_value = _value_after_label(lines, [r"swift", r"bic"])
  m = re.search(r"\b([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", swift_value or compact)
  if m:
    result["swift_code"] = m.group(1)

  score = _foreign_remittance_core_score(result)
  result["confidence"] = "high" if score >= 4 else ("medium" if score >= 2 else "low")
  return _normalize_foreign_remittance_result(result, parser="rule")


def parse_foreign_remittance_proof(text: str, api_key: str | None = None) -> dict:
  rule_result = parse_foreign_remittance_proof_rule_based(text)
  if _foreign_remittance_core_score(rule_result) >= 2:
    _get_logger().debug("Foreign remittance proof parser: rule-based success")
    return rule_result
  if api_key:
    try:
      return parse_foreign_remittance_proof_from_text(text, api_key)
    except Exception:
      _get_logger().exception("Foreign remittance proof parser: LLM fallback failed")
  return rule_result


def parse_foreign_remittance_proof_from_text(text: str, api_key: str) -> dict:
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": FOREIGN_REMITTANCE_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Extract foreign remittance proof details from this text:\n\n{text}",
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": FOREIGN_REMITTANCE_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return _normalize_foreign_remittance_result(result, parser="llm")
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM Foreign remittance proof : {str(e)}")


def parse_foreign_remittance_proof_from_image(img, api_key: str, max_side: int = 1600) -> dict:
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")
  try:
    from PIL import Image

    if not isinstance(img, Image.Image):
      raise ValueError("img must be a PIL.Image")
  except (ImportError, ValueError, TypeError):
    raise

  try:
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGB")
  except (AttributeError, ValueError, TypeError, OSError):
    pass
  try:
    w, h = img.size
    max_dim = max(w, h)
    if max_side and max_dim > max_side:
      scale = max_side / float(max_dim)
      img = img.resize((int(w * scale), int(h * scale)))
  except (AttributeError, ValueError, TypeError, OSError):
    pass

  buf = io.BytesIO()
  img.save(buf, format="PNG")
  b64 = base64.b64encode(buf.getvalue()).decode("ascii")
  data_url = f"data:image/png;base64,{b64}"

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": FOREIGN_REMITTANCE_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": [
            {
              "type": "text",
              "text": "Extract foreign remittance proof details from the image and return JSON.",
            },
            {"type": "image_url", "image_url": {"url": data_url}},
          ],
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": FOREIGN_REMITTANCE_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return _normalize_foreign_remittance_result(result, parser="vision")
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM vision foreign remittance parsing failed: {str(e)}")


# ===================== Business document parser ===================== #
BIZ_REG_SYSTEM_PROMPT = """
You are a U.S. business-profile data extraction assistant for an IP docketing and billing system.
Extract the schema fields from W-9 forms, IRS letters, state entity records, engagement forms, or similar business documents.

Rules:
- Dates should be YYYY-MM-DD when possible. If only month/year is available, use the first day of the month.
- Missing values must be empty strings.
- Prefer the email used for tax, billing, or accounting notices.
- reg_number should contain the U.S. Tax ID or EIN when present, preserving the standard two-digit/seven-digit EIN format when available.
- corp_registration_number should contain a state entity or registration number when distinct from the EIN.
"""

BIZ_REG_JSON_SCHEMA = {
  "name": "BusinessRegistration",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": [
      "reg_number",
      "company_name",
      "representative_name",
      "opening_date",
      "corp_registration_number",
      "business_location",
      "head_office_location",
      "business_type",
      "tax_invoice_email",
    ],
    "properties": {
      "reg_number": {"type": "string"},
      "company_name": {"type": "string"},
      "representative_name": {"type": "string"},
      "opening_date": {"type": "string"},
      "corp_registration_number": {"type": "string"},
      "business_location": {"type": "string"},
      "head_office_location": {"type": "string"},
      "business_type": {"type": "string"},
      "tax_invoice_email": {"type": "string"},
    },
  },
  "strict": True,
}


def _normalize_date_ymd(s: str) -> str:
  if not s:
    return ""
  import re

  # Replace separators and localized units
  t = s.strip()
  t = re.sub(r"[./\\]", "-", t)
  m = re.search(r"(\d{4})[- ]?(\d{1,2})[- ]?(\d{1,2})", t)
  if not m:
    return ""
  y, mo, d = m.group(1), m.group(2), m.group(3)
  try:
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
  except ValueError:
    return ""


def _format_business_reg_number(value: str) -> str:
  """
  Tax ID / EIN formatter.
  """
  import re

  if not value:
    return ""
  s = str(value).strip()
  if not s:
    return ""

  # Normalize common hyphen variants to '-'
  s = re.sub(r"[-\u2010\u2013\u2212]", "-", s)
  digits = re.sub(r"\D", "", s)
  if len(digits) == 9:
    return f"{digits[:2]}-{digits[2:]}"
  # If already formatted, keep as-is
  if re.fullmatch(r"\d{2}-\d{7}", s):
    return s
  return s


def _format_corp_registration_number(value: str) -> str:
  """
  Entity registration number formatter.
  """
  import re

  if not value:
    return ""
  s = str(value).strip()
  if not s:
    return ""

  s = re.sub(r"[-\u2010\u2013\u2212]", "-", s)
  digits = re.sub(r"\D", "", s)
  if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,30}", s):
    return s
  return s


def _normalize_business_registration_result(result: dict) -> dict:
  """
  Normalize Tax ID / EIN and entity-registration values, correcting swapped fields when detected.
  """
  import re

  if not isinstance(result, dict):
    return result

  reg_number = _format_business_reg_number(result.get("reg_number") or "")
  corp_registration_number = _format_corp_registration_number(
    result.get("corp_registration_number") or ""
  )

  def is_biz(s: str | None) -> bool:
    return bool(re.fullmatch(r"\d{2}-\d{7}", s or ""))

  def is_corp(s: str | None) -> bool:
    return bool(s and not is_biz(s) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,30}", s))

  # If swapped, fix
  if is_biz(corp_registration_number) and is_corp(reg_number):
    reg_number, corp_registration_number = corp_registration_number, reg_number
  elif not corp_registration_number and is_corp(reg_number):
    corp_registration_number, reg_number = reg_number, ""
  elif not reg_number and is_biz(corp_registration_number):
    reg_number, corp_registration_number = corp_registration_number, ""

  result["reg_number"] = reg_number
  result["corp_registration_number"] = corp_registration_number
  return result


def parse_business_registration_rule_based(text: str) -> dict:
  import re

  if not text:
    return {
      k: ""
      for k in (
        "reg_number",
        "company_name",
        "representative_name",
        "opening_date",
        "corp_registration_number",
        "business_location",
        "head_office_location",
        "business_type",
        "tax_invoice_email",
      )
    }
  T = text.replace("\r", "\n")

  # Pre-collapse spaces
  def find_after(label_patterns: list[str]) -> str:
    for pat in label_patterns:
      m = re.search(pat, T, re.IGNORECASE)
      if m:
        frag = T[m.end() : m.end() + 100]
        line = frag.split("\n", 1)[0]
        return line.strip(" :\t\u3000")
    return ""

  # Tax ID / EIN (e.g., 12-3456789)
  reg_number = ""
  m = re.search(
    r"(Tax ID / EIN|EIN|Employer Identification Number)[:\s]*([0-9]{2}[- ]?[0-9]{7})",
    T,
  )
  if m:
    reg_number = m.group(2)
  else:
    m = re.search(r"\b([0-9]{2}[- ]?[0-9]{7})\b", T)
    if m:
      reg_number = m.group(1)
  reg_number = _format_business_reg_number(reg_number)

  # Entity registration number.
  corp_registration_number = ""
  m = re.search(
    r"(Entity registration number|State registration number|File number)[:\s]*([A-Za-z0-9][A-Za-z0-9-]{3,30})",
    T,
    re.IGNORECASE,
  )
  if m:
    corp_registration_number = m.group(2)
  else:
    m = re.search(r"\b([A-Z]{1,4}[- ]?[0-9]{4,12})\b", T)
    if m:
      corp_registration_number = m.group(1)
  corp_registration_number = _format_corp_registration_number(corp_registration_number)

  # Company and representative.
  company_name = ""
  representative_name = ""
  v = find_after([r"(Business name|Legal business name|Company name)\s*[:\s]"])
  if v:
    company_name = v.split()[0:10]
    company_name = " ".join(company_name)
  v = find_after([r"(Representative|Authorized representative|Responsible party)\s*[:\s]"])
  if v:
    representative_name = " ".join(v.split()[0:6])

  # Business start date.
  opening_date = ""
  v = find_after([r"(Business start date|Formation date|Effective date|Start date)\s*[:\s]"])
  opening_date = _normalize_date_ymd(v)
  if not opening_date:
    m = re.search(r"(\d{4}[./-]\s*\d{1,2}[./-]\s*\d{1,2})", T)
    if m:
      opening_date = _normalize_date_ymd(m.group(1))

  # Locations.
  business_location = find_after([r"(Business address|Mailing address|Address)\s*[:\s]"])
  head_office_location = find_after([r"(Principal office|Head office|Registered office)\s*[:\s]"])

  # Business type.
  business_type = find_after([r"(Entity type|Business type|Business category|Business activity)\s*[:\s]"])

  # Tax email
  m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", T)
  tax_invoice_email = m.group(0) if m else ""

  return _normalize_business_registration_result(
    {
      "reg_number": reg_number,
      "company_name": company_name,
      "representative_name": representative_name,
      "opening_date": opening_date,
      "corp_registration_number": corp_registration_number,
      "business_location": business_location,
      "head_office_location": head_office_location,
      "business_type": business_type,
      "tax_invoice_email": tax_invoice_email,
    }
  )


def parse_business_registration(text: str, api_key: str) -> dict:
  # 1) Rule-based first (fast, no cost)
  rb = parse_business_registration_rule_based(text)
  filled = sum(1 for v in rb.values() if v)
  if filled >= 1:
    return rb
  # 2) Fallback to LLM structured output
  if api_key:
    return parse_business_registration_from_text(text, api_key)
  # No API key: return rule-based (possibly empty)
  return rb


def parse_business_registration_from_text(text: str, api_key: str) -> dict:
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")
  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": BIZ_REG_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Extract key information from the following business document text:\n\n{text}",
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": BIZ_REG_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return _normalize_business_registration_result(result)
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM business-document parsing failed: {str(e)}")


def parse_business_registration_from_image(img, api_key: str, max_side: int = 1600) -> dict:
  """
  Parse a business document image directly with an OpenAI vision model.

  Args:
    img: PIL.Image
    api_key: OpenAI API key.
    max_side: Maximum width or height before image submission.

  Returns:
    dict: Parsed business-profile fields matching BIZ_REG_JSON_SCHEMA.
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")

  if not api_key:
    raise ValueError("OpenAI API key is not configured.")
  try:
    from PIL import Image

    if not isinstance(img, Image.Image):
      raise ValueError("img must be a PIL.Image")
  except (ImportError, ValueError, TypeError):
    raise

  # Convert to RGB and downscale
  try:
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGB")
  except (AttributeError, ValueError, TypeError, OSError):
    pass
  try:
    w, h = img.size
    m = max(w, h)
    if max_side and m > max_side:
      scale = max_side / float(m)
      img = img.resize((int(w * scale), int(h * scale)))
  except (AttributeError, ValueError, TypeError, OSError):
    pass

  # Encode to PNG base64
  buf = io.BytesIO()
  img.save(buf, format="PNG")
  b64 = base64.b64encode(buf.getvalue()).decode("ascii")
  data_url = f"data:image/png;base64,{b64}"

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": BIZ_REG_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": [
            {
              "type": "text",
              "text": "Extract business-profile details from the image and return JSON.",
            },
            {"type": "image_url", "image_url": {"url": data_url}},
          ],
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": BIZ_REG_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return _normalize_business_registration_result(result)
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM business-document image parsing failed: {str(e)}")


# ===================== Application notice parser ===================== #
APP_NOTICE_SYSTEM_PROMPT = """
You extract identifiers from U.S. IP application notices for a U.S. docketing system.

Rules:
- Extract the internal reference number when present, for example 22PD0123US.
- Extract the official application number exactly as displayed.
- Extract applicant_name and agent_name when present.
- Missing values must be empty strings.
"""

APP_NOTICE_JSON_SCHEMA = {
  "name": "ApplicationNotice",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref_no", "app_no", "applicant_name", "agent_name"],
    "properties": {
      "ref_no": {"type": "string"},
      "app_no": {"type": "string"},
      "applicant_name": {"type": "string"},
      "agent_name": {"type": "string"},
    },
  },
  "strict": True,
}


def parse_application_notice_from_text(text: str, api_key: str) -> dict:
  """
  Extract application notice identifiers from OCR text.

  Args:
    text: Application notice OCR text.
    api_key: OpenAI API key.

  Returns:
    dict: {ref_no, app_no, applicant_name, agent_name}
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": APP_NOTICE_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Extract application notice identifiers from this text:\n\n{text}",
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": APP_NOTICE_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return result
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM application notice parsing failed: {str(e)}")


def parse_application_notice_from_image(img, api_key: str, max_side: int = 1600) -> dict:
  """
  Extract application notice identifiers from an image.
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  try:
    from PIL import Image

    if not isinstance(img, Image.Image):
      raise ValueError("img must be a PIL.Image")
  except (ImportError, ValueError, TypeError):
    raise

  # Convert and downscale
  try:
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGB")
  except (AttributeError, ValueError, TypeError, OSError):
    pass
  try:
    w, h = img.size
    m = max(w, h)
    if max_side and m > max_side:
      scale = max_side / float(m)
      img = img.resize((int(w * scale), int(h * scale)))
  except (AttributeError, ValueError, TypeError, OSError):
    pass

  # Encode to PNG base64
  buf = io.BytesIO()
  img.save(buf, format="PNG")
  b64 = base64.b64encode(buf.getvalue()).decode("ascii")
  data_url = f"data:image/png;base64,{b64}"

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": APP_NOTICE_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": [
            {
              "type": "text",
              "text": "Extract application notice identifiers from the image and return JSON.",
            },
            {"type": "image_url", "image_url": {"url": data_url}},
          ],
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": APP_NOTICE_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return result
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM application notice image parsing failed: {str(e)}")


# ===================== Payment confirmation parser ===================== #
PAYMENT_CONFIRM_SYSTEM_PROMPT = """
You extract identifiers from payment confirmations, fee receipts, and fee confirmation OCR
for a U.S. IP docketing system.

Rules:
- Extract the internal reference number when present, for example 22PD0123US.
- Extract the official application number exactly as displayed.
- Missing values must be empty strings.
"""

PAYMENT_CONFIRM_JSON_SCHEMA = {
  "name": "PaymentConfirmation",
  "schema": {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref_no", "app_no"],
    "properties": {
      "ref_no": {"type": "string"},
      "app_no": {"type": "string"},
    },
  },
  "strict": True,
}


def parse_payment_confirmation_from_text(text: str, api_key: str) -> dict:
  """
  Extract payment confirmation identifiers from OCR text.

  Args:
    text: Payment confirmation OCR text.
    api_key: OpenAI API key.

  Returns:
    dict: {ref_no, app_no}
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": PAYMENT_CONFIRM_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": f"Extract payment confirmation identifiers from this text:\n\n{text}",
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": PAYMENT_CONFIRM_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return result
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM payment confirmation parsing failed: {str(e)}")


def parse_payment_confirmation_from_image(img, api_key: str, max_side: int = 1600) -> dict:
  """
  Extract payment confirmation identifiers from an image.
  """
  if OpenAI is None:
    raise RuntimeError("OpenAI package is not installed. Install 'openai' to use LLM parsing.")
  if not api_key:
    raise ValueError("OpenAI API key is not configured.")

  try:
    from PIL import Image

    if not isinstance(img, Image.Image):
      raise ValueError("img must be a PIL.Image")
  except (ImportError, ValueError, TypeError):
    raise

  # Convert and downscale
  try:
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGB")
  except (AttributeError, ValueError, TypeError, OSError):
    pass
  try:
    w, h = img.size
    m = max(w, h)
    if max_side and m > max_side:
      scale = max_side / float(m)
      img = img.resize((int(w * scale), int(h * scale)))
  except (AttributeError, ValueError, TypeError, OSError):
    pass

  # Encode to PNG base64
  buf = io.BytesIO()
  img.save(buf, format="PNG")
  b64 = base64.b64encode(buf.getvalue()).decode("ascii")
  data_url = f"data:image/png;base64,{b64}"

  client = OpenAI(api_key=api_key)
  try:
    response = client.chat.completions.create(
      model=_billing_llm_model(),
      messages=[
        {"role": "system", "content": PAYMENT_CONFIRM_SYSTEM_PROMPT},
        {
          "role": "user",
          "content": [
            {
              "type": "text",
              "text": "Extract payment confirmation identifiers from the image and return JSON.",
            },
            {"type": "image_url", "image_url": {"url": data_url}},
          ],
        },
      ],
      response_format={
        "type": "json_schema",
        "json_schema": PAYMENT_CONFIRM_JSON_SCHEMA,
      },
      temperature=0,
    )
    import json as _json

    result = _json.loads(response.choices[0].message.content)
    return result
  except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
    raise Exception(f"LLM payment confirmation image parsing failed: {str(e)}")
