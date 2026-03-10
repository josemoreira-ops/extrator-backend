import io
import re
import pdfplumber
from pypdf import PdfReader, PdfWriter
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allows the GitHub Pages frontend to call this backend

PDF_PASSWORD = "1231"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def decrypt_pdf(file_bytes: bytes) -> bytes:
    """Return decrypted PDF bytes. If already unlocked, returns as-is."""
    reader = PdfReader(io.BytesIO(file_bytes))

    if not reader.is_encrypted:
        return file_bytes  # nothing to do

    # Try default password
    result = reader.decrypt(PDF_PASSWORD)
    if result == 0:
        raise ValueError(f"PDF está protegido e a senha '{PDF_PASSWORD}' não funcionou.")

    # Re-write to a clean, unlocked buffer
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def parse_brl(text: str) -> float | None:
    """
    Parse a BRL-prefixed or plain monetary string to float.
    Accepts: 'BRL634,25', 'BRL1.234,56', '634,25', '1.234,56'
    Returns None if not a valid monetary value.
    """
    if not text:
        return None
    clean = re.sub(r"^BRL", "", text.strip(), flags=re.IGNORECASE)
    # Brazilian format: 1.234,56
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", clean):
        return float(clean.replace(".", "").replace(",", "."))
    # Plain decimal: 634.25
    if re.fullmatch(r"\d+\.\d{2}", clean):
        return float(clean)
    return None


def fmt_brl(value: float) -> str:
    """Format float to Brazilian currency string '1.234,56'."""
    formatted = f"{value:,.2f}"          # 1,234.56 (US format)
    # Swap separators to Brazilian
    formatted = formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    return formatted


def is_name(text: str) -> bool:
    """Heuristic: does this string look like a person's name?"""
    if not text or len(text) < 4:
        return False
    words = [w for w in text.split() if re.fullmatch(r"[A-Za-zÀ-ÿ]{2,}", w)]
    if len(words) < 2:
        return False
    letter_ratio = len(re.findall(r"[A-Za-zÀ-ÿ]", text)) / max(len(text), 1)
    return letter_ratio > 0.6


def title_case(text: str) -> str:
    LOWER = {"de", "da", "do", "dos", "das", "e", "em", "a", "o", "as", "os", "na", "no"}
    words = text.lower().split()
    result = []
    for i, w in enumerate(words):
        result.append(w if (i > 0 and w in LOWER) else w.capitalize())
    return " ".join(result)


# ─────────────────────────────────────────────────────────────────────────────
# Core extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_page(page) -> list[dict]:
    """
    Extract (name, valor) pairs from a single pdfplumber page.

    Strategy:
    1. Collect every word with its bounding box (x0, top, x1, bottom).
    2. Find all BRL-prefixed monetary words → these are value columns.
    3. The RIGHTMOST cluster of monetary words = "Valor R$" column.
    4. The LEFTMOST cluster of text words = "Nome do segurado" column.
    5. For each Valor R$ cell, find all name-column words within the same
       vertical band (±ROW_HALF px) and assemble the full name.
    """
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    if not words:
        return []

    page_width = page.width

    # ── Step 1: identify all BRL / monetary words ────────────────────────────
    brl_re = re.compile(r"^BRL\d", re.IGNORECASE)
    money_re = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")

    monetary = [w for w in words if brl_re.match(w["text"]) or money_re.match(w["text"])]

    if not monetary:
        return []

    # ── Step 2: find the rightmost X cluster = "Valor R$" ────────────────────
    max_x0 = max(w["x0"] for w in monetary)
    # Allow 4% tolerance to group near-right items together
    valor_x_min = max_x0 - page_width * 0.04
    valor_words = [w for w in monetary if w["x0"] >= valor_x_min]

    # ── Step 3: name column = leftmost 18% of page width ─────────────────────
    name_x_max = page_width * 0.18
    name_pool = [
        w for w in words
        if w["x0"] <= name_x_max
        and not re.fullmatch(r"\d+", w["text"])   # skip pure numbers
        and len(w["text"]) >= 2
    ]

    ROW_HALF = 25  # vertical tolerance in points (≈ 2–3 text lines)

    used_name_keys = set()
    results = []

    # Sort valor words top-to-bottom (pdfplumber: top = distance from page top)
    valor_words.sort(key=lambda w: w["top"])

    for vw in valor_words:
        valor_float = parse_brl(vw["text"])
        if valor_float is None:
            continue  # skip header labels like "Valor R$"

        v_mid = (vw["top"] + vw["bottom"]) / 2

        # Collect name parts in the same horizontal band, not yet consumed
        name_parts = [
            nw for nw in name_pool
            if abs((nw["top"] + nw["bottom"]) / 2 - v_mid) <= ROW_HALF
            and (nw["top"], nw["x0"]) not in used_name_keys
        ]

        if not name_parts:
            continue

        # Sort top-to-bottom, then left-to-right within same Y
        name_parts.sort(key=lambda w: (round(w["top"]), w["x0"]))
        name_raw = " ".join(nw["text"] for nw in name_parts)

        if not is_name(name_raw):
            continue

        results.append({
            "nome": title_case(name_raw),
            "valor": fmt_brl(valor_float),
            "valor_float": valor_float,
        })

        for nw in name_parts:
            used_name_keys.add((nw["top"], nw["x0"]))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/extract", methods=["POST"])
def extract():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Apenas arquivos .pdf são aceitos."}), 400

    try:
        raw_bytes = file.read()
        pdf_bytes = decrypt_pdf(raw_bytes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": f"Erro ao abrir o PDF: {e}"}), 500

    records = []
    page_log = []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                page_records = extract_page(page)
                records.extend(page_records)
                page_log.append({"page": i, "found": len(page_records)})
    except Exception as e:
        return jsonify({"error": f"Erro durante a extração: {e}"}), 500

    total = sum(r["valor_float"] for r in records)

    # Remove internal float before sending to client
    clean_records = [{"nome": r["nome"], "valor": r["valor"]} for r in records]

    return jsonify({
        "records": clean_records,
        "total": fmt_brl(total),
        "count": len(clean_records),
        "pages": page_log,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
