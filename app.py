from datetime import datetime, timezone
import io
import os
import re
import zipfile
from flask import Flask, jsonify, render_template_string, request
import pdfplumber
import requests

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

CELIGO_WEBHOOK_URL = os.environ.get(
    "CELIGO_WEBHOOK_URL",
    "https://api.integrator.io/v1/exports/6a3e22e548c8b4a733fbeb15/KVk2DW2JtJkffDcxDfAx0o2S0mwcSyXP/data",
)

with open("templates/index.html", "r", encoding="utf-8") as f:
  HTML_TEMPLATE = f.read()


@app.route("/")
def index():
  return render_template_string(HTML_TEMPLATE)


def parse_mvp_document(file_stream, filename):
  with pdfplumber.open(file_stream) as pdf:
    full_text = "\n".join(
        [page.extract_text() for page in pdf.pages if page.extract_text()]
    )

  if not full_text.strip():
    raise ValueError(f"'{filename}' has no extractable text.")

  data = {
      "filename": filename,
      "order_no": None,
      "order_date": None,
      "delivery_date": None,
      "customer_id": "MV5278",
      "currency": "USD",
      "customer_p_o_no": None,
      "terms": "Net 30 Days",
      "contact": "Admin,admin",
      "line_items": [],
      "sales_total": 0.0,
      "freight_misc": 0.0,
      "tax_total": 0.0,
      "disc_total": 0.0,
      "total_usd": 0.0,
  }

  lines = [line.strip() for line in full_text.split("\n") if line.strip()]

  # 1. Precise Header Metadata Extraction
  for idx, line in enumerate(lines):
    upper_line = line.upper()

    # Order Number (Ensuring company name "Mor-Value" is ignored)
    if "ORDER NO." in upper_line or "INVOICE NBR" in upper_line:
      order_match = re.search(r"(?:Order\s*No\.?|Invoice\s*Nbr\.?)\s*[:\|]?\s*([0-9]+)", line, re.IGNORECASE)
      if order_match:
        data["order_no"] = order_match.group(1).strip()
      elif idx + 1 < len(lines):
        next_nums = re.findall(r"\b\d{5,}\b", lines[idx + 1])
        if next_nums:
          data["order_no"] = next_nums[0]

    # Order Date
    if "ORDER DATE" in upper_line or "INVOICE DATE" in upper_line:
      date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", line)
      if date_match:
        data["order_date"] = date_match.group(1)
        data["delivery_date"] = data["order_date"]
      elif idx + 1 < len(lines):
        date_match_next = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", lines[idx + 1])
        if date_match_next:
          data["order_date"] = date_match_next.group(1)
          data["delivery_date"] = data["order_date"]

    # Delivery Date
    if "DELIVERY DATE" in upper_line:
      date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", line)
      if date_match:
        data["delivery_date"] = date_match.group(1)

    # Customer ID
    if "CUSTOMER ID" in upper_line:
      parts = line.split()
      for p in parts:
        if p.startswith("MV") or (p.isalnum() and len(p) > 3):
          data["customer_id"] = p

  # Precise Customer PO Lookup (Targeting numeric PO values beneath header labels)
  for idx, line in enumerate(lines):
    if "CUSTOMER P.O. NO." in line.upper() or "CUST. PO" in line.upper():
      if idx + 1 < len(lines):
        candidate_tokens = lines[idx + 1].split()
        for token in candidate_tokens:
          # A valid PO code is typically a numeric string or alphanumeric code, never labels like TERMS/CONTACT
          if token.upper() not in ["TERMS", "CONTACT", "FOB", "NET", "SHIPPING"] and len(token) >= 4:
            data["customer_p_o_no"] = token
            break

  # Ultimate Fallback regex for standard MV numbers if label line-lookups fail
  if not data["customer_p_o_no"]:
    alt_po = re.search(r"\b(445\d{4})\b", full_text)
    if alt_po:
      data["customer_p_o_no"] = alt_po.group(1).strip()

  # 2. Extract Totals Block First (Mathematical Anchors)
  sales_match = re.search(r"Sales\s*Total\s*[:\|]?\s*\$?([\d,]+\.\d{2})", full_text, re.IGNORECASE)
  if sales_match:
    data["sales_total"] = float(sales_match.group(1).replace(",", ""))

  total_match = re.search(r"(?:Total\s*\(USD\)|Invoice\s*Total|Balance)\s*[:\|]?\s*\$?([\d,]+\.\d{2})", full_text, re.IGNORECASE)
  if total_match:
    data["total_usd"] = float(total_match.group(1).replace(",", ""))
  else:
    data["total_usd"] = data["sales_total"]

  # 3. Line Items Scraper
  item_counter = 1
  i = 0
  while i < len(lines):
    line = lines[i]
    price_match = re.search(
        r"^([\d.]+)\s+(?:EACH|PCS)\s+([$\d,]+\.\d{2})\s+([$\d,]+\.\d{2})$",
        line,
        re.IGNORECASE,
    )
    if price_match:
      qty_str, price_str, ext_str = price_match.groups()
      
      desc_lines = []
      j = i - 1
      while j >= 0 and not re.match(r"^\d+$", lines[j]) and "NOTE:" not in lines[j].upper():
        if not re.search(r"(?:OEM|PRICE|UOM|ITEM|NO\.)", lines[j], re.IGNORECASE):
          desc_lines.insert(0, lines[j])
        j -= 1
      
      item_desc = " ".join(desc_lines).strip() or f"Part Item {item_counter}"

      data["line_items"].append({
          "no": str(item_counter),
          "oem_p_n": "",
          "item": item_desc,
          "qty": float(qty_str),
          "uom": "EACH",
          "price": float(price_str.replace("$", "").replace(",", "")),
          "ext_price": float(ext_str.replace("$", "").replace(",", "")),
      })
      item_counter += 1
    i += 1

  if not data["line_items"] and data["sales_total"] > 0:
    data["line_items"].append({
        "no": "1",
        "oem_p_n": "GENERIC",
        "item": "Synthetic Order Balance Item",
        "qty": 1.0,
        "uom": "EACH",
        "price": data["sales_total"],
        "ext_price": data["sales_total"],
    })

  return data


@app.route("/api/parse-documents", methods=["POST"])
def parse_documents():
  if "files" not in request.files:
    return jsonify({"success": False, "error": "No files found"}), 400

  files = request.files.getlist("files")
  all_records = []

  for file in files:
    if file.filename.endswith(".zip"):
      with zipfile.ZipFile(file, "r") as z:
        for name in z.namelist():
          if name.lower().endswith(".pdf"):
            with z.open(name) as f:
              all_records.append(
                  parse_mvp_document(io.BytesIO(f.read()), name.split("/")[-1])
              )
    elif file.filename.endswith(".pdf"):
      all_records.append(parse_mvp_document(file.stream, file.filename))

  payload = {
      "batch_id": f"BATCH-{int(datetime.now(timezone.utc).timestamp())}",
      "records": all_records,
      "total_records": len(all_records),
  }
  return jsonify({"success": True, "data": payload})


@app.route("/api/send-to-celigo", methods=["POST"])
def send_to_celigo():
  try:
    payload = request.get_json()
    resp = requests.post(CELIGO_WEBHOOK_URL, json=payload)
    return jsonify({"success": True, "celigo_status": resp.status_code})
  except Exception as e:
    return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=3000, debug=True)
