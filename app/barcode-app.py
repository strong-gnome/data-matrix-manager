"""
Barcode Management System
=========================
Docker:  docker build -t barcode-mgr . && docker compose up -d
Manual:  pip install -r requirements.txt && python barcode-app.py
"""

import os, io, json, sqlite3, logging, time, shutil, re, configparser, threading
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

import cv2
import numpy as np
from PIL import Image
from flask import (
    Flask, request, jsonify, Response,
    redirect, url_for, send_file
)
from pdf2image import convert_from_path, pdfinfo_from_path
from pylibdmtx.pylibdmtx import decode as dmtx_decode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).parent
DATA_DIR = Path("/data")
UPLOAD_DIR = DATA_DIR / "original_batches"
BARCODE_IMG_DIR = DATA_DIR / "barcode_images"
DB_PATH = DATA_DIR / "barcodes.db"
TO_PRINT_DIR = DATA_DIR / "to_print"
TO_REPRINT_DIR = DATA_DIR / "to_reprint"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

_cfg = configparser.ConfigParser()
_cfg_path = APP_DIR / "config.ini"
if _cfg_path.exists():
    _cfg.read(_cfg_path)
    log.info("Loaded config from %s", _cfg_path)
else:
    log.warning("config.ini not found at %s — using defaults", _cfg_path)

_s = _cfg["barcode"] if "barcode" in _cfg else {}
# Size of barcode squares as they appear in the source PDF (mm)
SOURCE_BARCODE_SIZE_MM = float(_s.get("source_barcode_size_mm", 10))
# Size of the barcode square on the printed label (mm)
PRINT_BARCODE_SIZE_MM  = float(_s.get("print_barcode_size_mm",  15))
# Total paper/label size (mm) — square label
PRINT_PAPER_SIZE_MM    = float(_s.get("print_paper_size_mm",    25))
# Gap between sequence number and barcode
SEQ_NUM_GAP_MM = float(_s.get("seq_num_gap_mm", 2.0))

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
BARCODE_IMG_DIR.mkdir(exist_ok=True)
TO_PRINT_DIR.mkdir(exist_ok=True)
TO_REPRINT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _sanitize(s):
    """Remove GS (\x1D) and other control chars, collapse multiple spaces."""
    s = s.replace('\x1D', '')
    return re.sub(r' +', ' ', re.sub(r'[^\x20-\x7E\x80-\xFF]', ' ', s)).strip()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_BATCH_ID_RE = re.compile(r"^batch_(\d+)_")
_BATCH_ID_FULL_RE = re.compile(r"^batch_\d+_\d{4}-\d{2}-\d{2}$")


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL,
                created_at TEXT NOT NULL, status TEXT DEFAULT 'uploaded',
                total_pages INTEGER DEFAULT 0, pages_done INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS barcodes (
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                seq_num INTEGER, decoded TEXT, img_path TEXT,
                status TEXT DEFAULT 'decoded', scanned_at TEXT,
                notes TEXT, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bc_batch ON barcodes(batch_id);
            CREATE INDEX IF NOT EXISTS idx_bc_decoded ON barcodes(decoded);
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
        """)
        # Seed the batch counter from existing batch IDs so numbers keep
        # increasing on DBs created before the counter existed.
        max_num = 0
        for r in db.execute("SELECT id FROM batches").fetchall():
            m = _BATCH_ID_RE.match(r["id"])
            if m:
                max_num = max(max_num, int(m.group(1)))
        db.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('batch', 0)")
        db.execute("UPDATE counters SET value=MAX(value, ?) WHERE name='batch'", (max_num,))
        # Migrate DBs created before these columns existed.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(batches)").fetchall()}
        if "total_pages" not in cols:
            db.execute("ALTER TABLE batches ADD COLUMN total_pages INTEGER DEFAULT 0")
        if "pages_done" not in cols:
            db.execute("ALTER TABLE batches ADD COLUMN pages_done INTEGER DEFAULT 0")
        if "dup_in_batch" not in cols:
            db.execute("ALTER TABLE batches ADD COLUMN dup_in_batch INTEGER DEFAULT 0")
        if "dup_other" not in cols:
            db.execute("ALTER TABLE batches ADD COLUMN dup_other INTEGER DEFAULT 0")
        if "dup_batches" not in cols:
            db.execute("ALTER TABLE batches ADD COLUMN dup_batches TEXT")
        # A batch still 'processing' at startup was cut off by a restart or
        # crash; its background thread is gone, so it would never finish.
        stuck = db.execute("UPDATE batches SET status='error' WHERE status='processing'").rowcount
        if stuck:
            log.warning("Marked %d batch(es) interrupted during processing as 'error'", stuck)


def _valid_batch_id(batch_id):
    """Batch IDs are used in file paths, so only accept the generated format."""
    return isinstance(batch_id, str) and _BATCH_ID_FULL_RE.match(batch_id) is not None


def _batch_exists(db, batch_id):
    return _valid_batch_id(batch_id) and db.execute(
        "SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone() is not None


def _json_body():
    return request.get_json(silent=True) or {}


def find_duplicates(db, batch_id):
    """
    Count codes in this batch that appear more than once in it, or that also
    exist in another batch. Honest Sign codes must be unique per product, so
    either case means the same code could end up on two products.
    """
    dup_in_batch = db.execute(
        "SELECT COALESCE(SUM(n - 1), 0) AS extra FROM ("
        "  SELECT COUNT(*) AS n FROM barcodes WHERE batch_id=? AND decoded IS NOT NULL"
        "  GROUP BY decoded HAVING COUNT(*) > 1)", (batch_id,)).fetchone()["extra"]
    rows = db.execute(
        "SELECT b2.batch_id AS other, COUNT(DISTINCT b1.decoded) AS n "
        "FROM barcodes b1 JOIN barcodes b2 ON b2.decoded = b1.decoded AND b2.batch_id != b1.batch_id "
        "WHERE b1.batch_id=? AND b1.decoded IS NOT NULL GROUP BY b2.batch_id ORDER BY b2.batch_id",
        (batch_id,)).fetchall()
    dup_other = db.execute(
        "SELECT COUNT(DISTINCT b1.decoded) AS n FROM barcodes b1 "
        "JOIN barcodes b2 ON b2.decoded = b1.decoded AND b2.batch_id != b1.batch_id "
        "WHERE b1.batch_id=? AND b1.decoded IS NOT NULL", (batch_id,)).fetchone()["n"]
    return dup_in_batch, dup_other, ",".join(r["other"] for r in rows) or None


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------
def get_page_count(filepath):
    """Read page count from the PDF without rasterizing anything."""
    info = pdfinfo_from_path(filepath)
    return info["Pages"]


def render_page(filepath, page_num, dpi=400):
    """
    Render a single page to a cv2 (BGR) image.
    Only that one page is rasterized/held in memory at a time — poppler is
    told to render just [page_num, page_num] via first_page/last_page,
    so this does not touch the rest of the document.
    """
    imgs = convert_from_path(filepath, dpi=dpi, first_page=page_num, last_page=page_num)
    if not imgs:
        return None
    pil_img = imgs[0]
    cv_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    pil_img.close()
    del imgs
    return cv_img


def extract_barcode_regions_dynamic(page_img):
    """
    Candidate-region search used only as a fallback (see find_barcodes_in_page)
    when a direct full-page libdmtx scan doesn't find anything — e.g. a very
    busy/noisy page that defeats libdmtx's own symbol finder.

    Unlike the old implementation, the area/aspect bounds used to keep or
    reject a contour are derived from the contours actually found *on this
    page* (their median size) rather than a fixed percentage of the total
    page area. A fixed percentage only works for the one barcode/page size
    ratio the app happened to be tuned against; documents with a different
    ratio (bigger barcode relative to a small label, lots of margin, etc.)
    would have every real candidate filtered out. Sizing the bounds off the
    page's own contour population instead adapts automatically to whatever
    layout the PDF actually uses.
    """
    gray = cv2.cvtColor(page_img, cv2.COLOR_BGR2GRAY)
    h_page, w_page = page_img.shape[:2]
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 51, 15)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (w_page // 8, 1))
    h_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, hk)
    no_h = cv2.subtract(thresh, h_lines)
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, h_page // 8))
    v_lines = cv2.morphologyEx(no_h, cv2.MORPH_OPEN, vk)
    no_lines = cv2.subtract(no_h, v_lines)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(no_lines, cv2.MORPH_CLOSE, kernel, iterations=4)
    dilated = cv2.dilate(closed, kernel, iterations=2)
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area <= 0:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        aspect = max(w, h) / max(min(w, h), 1)
        boxes.append((x, y, w, h, area, aspect))

    if not boxes:
        log.info("  Contour scan: 0 candidates")
        return []

    # Data Matrix symbols are square; decorative borders/text blocks usually
    # aren't, so prefer near-square blobs when they exist.
    square_boxes = [b for b in boxes if b[5] <= 1.8]
    pool = square_boxes or boxes

    areas = sorted(b[4] for b in pool)
    median_area = areas[len(areas) // 2]
    # Accept anything reasonably close to the page's own median candidate
    # size instead of an absolute, page-size-relative constant.
    min_area = median_area * 0.3
    max_area = median_area * 3.0

    candidates = [b for b in square_boxes if min_area <= b[4] <= max_area]
    if not candidates:
        candidates = square_boxes or pool

    log.info("  Contour scan: %d total, %d candidates (median_area=%.0f)",
              len(contours), len(candidates), median_area)
    candidates.sort(key=lambda b: (b[1], b[0]))
    regions = []
    pad = 10
    for x, y, w, h, area, aspect in candidates:
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w_page, x + w + pad), min(h_page, y + h + pad)
        regions.append(page_img[y0:y1, x0:x1])
    return regions


def decode_datamatrix(img, timeout_ms=3000):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    sharp = cv2.filter2D(gray, -1, np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]]))
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for attempt in [sharp, gray, otsu]:
        pil = Image.fromarray(attempt)
        results = dmtx_decode(pil, timeout=timeout_ms)
        if results:
            raw = results[0].data
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                decoded = raw.decode("latin-1")
            return _sanitize(decoded)
    return None


def find_barcodes_in_page(page_img, timeout_ms=3000, pad=10, max_count=25):
    """
    Locate *and* decode every Data Matrix symbol on a page.

    Primary strategy: hand the whole page straight to libdmtx and let its
    own symbol finder locate every barcode on it. This is the "dynamic" path
    — libdmtx looks for the Data Matrix finder pattern itself, so it works
    regardless of how big the barcode is relative to the page, how much
    white margin surrounds it, or how many barcodes are on the page. The
    previous approach pre-filtered OpenCV contours using a min/max area
    expressed as a fixed percentage of total page area; that only matched
    the one batch format it happened to be tuned on. Anything with a
    different barcode-to-page size ratio (like this PDF, where the barcode
    covers ~22% of the page rather than the old max of 8%) had every real
    candidate discarded before decoding was even attempted.

    Fallback: if the full-page scan finds nothing (can happen on a very
    busy/noisy page), fall back to the old contour-based candidate search,
    now with dynamically-derived area bounds (see
    extract_barcode_regions_dynamic) instead of a static percentage.
    """
    h_page, w_page = page_img.shape[:2]
    gray = cv2.cvtColor(page_img, cv2.COLOR_BGR2GRAY) if len(page_img.shape) == 3 else page_img

    found = []
    try:
        results = dmtx_decode(Image.fromarray(gray), timeout=timeout_ms, max_count=max_count)
    except Exception as e:
        log.warning("  Full-page dmtx scan failed (%s)", e)
        results = []

    for r in results:
        left, top, w, h = r.rect.left, r.rect.top, r.rect.width, r.rect.height
        # libdmtx reports rect with a bottom-left origin; flip to the
        # top-left origin used by our (and PIL/cv2's) image arrays.
        x0 = max(0, left - pad)
        x1 = min(w_page, left + w + pad)
        y0 = max(0, h_page - top - h - pad)
        y1 = min(h_page, h_page - top + pad)
        region = page_img[y0:y1, x0:x1]
        try:
            decoded = r.data.decode("utf-8")
        except UnicodeDecodeError:
            decoded = r.data.decode("latin-1")
        found.append((region, _sanitize(decoded)))

    if found:
        log.info("  Full-page dmtx scan: %d barcode(s) found directly", len(found))
        return found

    log.info("  Full-page dmtx scan found nothing — falling back to contour search")
    regions = extract_barcode_regions_dynamic(page_img)
    return [(region, decode_datamatrix(region, timeout_ms=timeout_ms)) for region in regions]


# ---------------------------------------------------------------------------
# PDF label generation
# ---------------------------------------------------------------------------
MM_TO_PT = 72.0 / 25.4  # 1 mm = 72/25.4 points


def save_barcodes_as_pdf(rows, output_path):
    """
    Render multiple barcode rows into a single multi-page PDF (1 barcode per page).
    rows: list of dicts with keys img_path, seq_num
    output_path: full Path to the output .pdf file
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader

    paper_mm = PRINT_PAPER_SIZE_MM
    bc_mm    = PRINT_BARCODE_SIZE_MM
    gap_mm   = SEQ_NUM_GAP_MM
    h_margin_mm = (paper_mm - bc_mm) / 2.0
    bc_y_mm = (paper_mm - bc_mm) / 2.0  # barcode centered vertically on the page
    font_size_pt = max(6.0, gap_mm * MM_TO_PT * 0.8)  # font scaled to the gap

    page_w = paper_mm * mm
    page_h = paper_mm * mm

    c = canvas.Canvas(str(output_path), pagesize=(page_w, page_h))
    for i, row in enumerate(rows):
        if i > 0:
            c.showPage()
        img_reader = ImageReader(row["img_path"])
        c.drawImage(
            img_reader,
            h_margin_mm * mm,
            bc_y_mm * mm,
            width=bc_mm * mm,
            height=bc_mm * mm,
            preserveAspectRatio=False,
        )
        c.setFont("Helvetica-Bold", font_size_pt)
        c.drawCentredString(
            h_margin_mm * mm + (bc_mm / 2.0) * mm,  # horizontally centered under barcode
            (bc_y_mm - gap_mm) * mm,                 # gap_mm below the barcode bottom
            str(row["seq_num"])
        )
    c.save()
    log.info("Saved multi-page PDF: %s (%d pages)", output_path, len(rows))


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
class _BatchDeleted(Exception):
    pass


def process_batch(batch_id, save_path):
    """
    Runs in a background thread. Renders and decodes one page at a time,
    updating batches.pages_done after each page so the frontend can poll
    for progress. Keeps memory flat regardless of PDF size (see render_page).

    Any unexpected failure marks the batch 'error' so it never stays stuck in
    'processing'. If the batch is deleted while running, processing stops.
    """
    try:
        _process_batch(batch_id, save_path)
    except _BatchDeleted:
        log.info("Batch %s: deleted during processing — stopped", batch_id)
    except Exception:
        log.exception("Batch %s: processing failed", batch_id)
        with get_db() as db:
            db.execute("UPDATE batches SET status='error' WHERE id=?", (batch_id,))


def _process_batch(batch_id, save_path):
    try:
        page_count = get_page_count(str(save_path))
    except Exception as e:
        log.error("Batch %s: failed to read PDF (%s)", batch_id, e)
        with get_db() as db:
            db.execute("UPDATE batches SET status='error' WHERE id=?", (batch_id,))
        return

    with get_db() as db:
        db.execute("UPDATE batches SET total_pages=? WHERE id=?", (page_count, batch_id))

    seq = 0
    for pi in range(page_count):
        try:
            page = render_page(str(save_path), pi + 1)
        except Exception as e:
            log.error("Batch %s page %d: failed to render (%s) — skipping", batch_id, pi + 1, e)
            page = None
        found = find_barcodes_in_page(page) if page is not None else []
        if page is not None:
            log.info("Batch %s page %d: %d regions", batch_id, pi + 1, len(found))
        with get_db() as db:
            # Hold the write lock while checking the batch still exists and
            # writing its rows/images, so a concurrent delete can't leave
            # orphans behind.
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone():
                raise _BatchDeleted()
            for ri, (region, decoded) in enumerate(found):
                seq += 1
                bc_id = "%s-%02d-%03d" % (batch_id, pi, ri)
                img_path = BARCODE_IMG_DIR / batch_id / (bc_id + ".png")
                if not cv2.imwrite(str(img_path), region):
                    raise IOError("could not write %s" % img_path)
                db.execute(
                    "INSERT INTO barcodes (id,batch_id,seq_num,decoded,img_path,status,created_at) VALUES (?,?,?,?,?,?,?)",
                    (bc_id, batch_id, seq, decoded, str(img_path),
                     "decoded" if decoded else "decode_failed", _utcnow()))
            db.execute("UPDATE batches SET pages_done=? WHERE id=?", (pi + 1, batch_id))
        del page, found

    with get_db() as db:
        dup_in_batch, dup_other, dup_batches = find_duplicates(db, batch_id)
        db.execute(
            "UPDATE batches SET status='processed', dup_in_batch=?, dup_other=?, dup_batches=? WHERE id=?",
            (dup_in_batch, dup_other, dup_batches, batch_id))
    if dup_in_batch or dup_other:
        log.warning("Batch %s: %d repeated code(s) within batch, %d code(s) also in %s",
                    batch_id, dup_in_batch, dup_other, dup_batches)
    log.info("Batch %s: processing complete (%d pages)", batch_id, page_count)


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error="File is too large (max 50 MB)"), 413


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file provided"), 400
    ext = Path(f.filename).suffix.lower()
    # Only PDF: pages are rendered with poppler, which cannot read EPS.
    if ext != ".pdf":
        return jsonify(error="Only PDF files accepted"), 400
    # Reserve a batch number from a counter that only ever increases, so
    # numbers are never reused after a batch is deleted. BEGIN IMMEDIATE
    # takes the write lock up front, so parallel uploads get distinct numbers.
    batch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        while True:
            db.execute("UPDATE counters SET value=value+1 WHERE name='batch'")
            batch_num = db.execute("SELECT value FROM counters WHERE name='batch'").fetchone()["value"]
            batch_id = "batch_%d_%s" % (batch_num, batch_date)
            if not db.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone():
                break
        db.execute(
            "INSERT INTO batches (id,filename,created_at,status,total_pages,pages_done) VALUES (?,?,?,?,?,?)",
            (batch_id, f.filename, _utcnow(), "processing", 0, 0))

    # Write files only after the ID is safely reserved, so an existing
    # batch's source file can never be overwritten.
    save_path = UPLOAD_DIR / (batch_id + ext)
    try:
        f.save(save_path)
        (BARCODE_IMG_DIR / batch_id).mkdir(exist_ok=True)
    except Exception as e:
        log.error("Batch %s: failed to save upload (%s)", batch_id, e)
        save_path.unlink(missing_ok=True)
        with get_db() as db:
            db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
        return jsonify(error="Failed to save uploaded file"), 500
    threading.Thread(target=process_batch, args=(batch_id, save_path), daemon=True).start()
    return jsonify(batch_id=batch_id, status="processing")


@app.route("/api/upload_progress/<batch_id>")
def api_upload_progress(batch_id):
    with get_db() as db:
        batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            return jsonify(error="Batch not found"), 404
        counts = db.execute(
            "SELECT "
            "SUM(CASE WHEN decoded IS NOT NULL THEN 1 ELSE 0 END) AS decoded, "
            "SUM(CASE WHEN decoded IS NULL THEN 1 ELSE 0 END) AS failed "
            "FROM barcodes WHERE batch_id=?", (batch_id,)).fetchone()
    return jsonify(
        status=batch["status"],
        total_pages=batch["total_pages"] or 0,
        pages_done=batch["pages_done"] or 0,
        decoded=counts["decoded"] or 0,
        failed=counts["failed"] or 0,
        dup_in_batch=batch["dup_in_batch"] or 0,
        dup_other=batch["dup_other"] or 0,
        dup_batches=batch["dup_batches"],
    )


@app.route("/api/batches")
def api_batches():
    with get_db() as db:
        rows = db.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/barcodes/<batch_id>")
def api_barcodes(batch_id):
    with get_db() as db:
        rows = db.execute("SELECT * FROM barcodes WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/barcode_image/<bc_id>")
def api_barcode_image(bc_id):
    with get_db() as db:
        row = db.execute("SELECT img_path FROM barcodes WHERE id=?", (bc_id,)).fetchone()
    if not row or not row["img_path"]:
        return "Not found", 404
    return send_file(row["img_path"], mimetype="image/png")


@app.route("/api/print", methods=["POST"])
def api_print():
    data = _json_body()
    batch_id = data.get("batch_id")
    bc_ids = data.get("barcode_ids", "all")
    if bc_ids != "all" and not (isinstance(bc_ids, list) and all(isinstance(i, str) for i in bc_ids)):
        return jsonify(error="barcode_ids must be \"all\" or a list of IDs"), 400
    with get_db() as db:
        if not _batch_exists(db, batch_id):
            return jsonify(error="Batch not found"), 404
        if bc_ids == "all":
            rows = db.execute("SELECT * FROM barcodes WHERE batch_id=? AND decoded IS NOT NULL ORDER BY seq_num", (batch_id,)).fetchall()
        elif bc_ids:
            ph = ",".join("?" * len(bc_ids))
            rows = db.execute(
                "SELECT * FROM barcodes WHERE batch_id=? AND decoded IS NOT NULL AND id IN (%s) ORDER BY seq_num" % ph,
                [batch_id] + bc_ids).fetchall()
        else:
            rows = []
        rows = [dict(r) for r in rows]
        saved = 0
        folder = pdf_url = None
        if rows:
            out_dir = TO_PRINT_DIR / batch_id
            out_dir.mkdir(exist_ok=True)
            out_path = out_dir / ("%s_print.pdf" % batch_id)
            try:
                save_barcodes_as_pdf(rows, out_path)
                saved = len(rows)
                folder = str(out_dir.resolve())
                pdf_url = url_for("api_print_pdf", batch_id=batch_id)
                # Never move an already-scanned code back to 'printed'.
                for row in rows:
                    db.execute("UPDATE barcodes SET status='printed' WHERE id=? AND status!='scanned'", (row["id"],))
            except Exception as e:
                log.error("PDF save failed: %s", e)
    return jsonify(printed=saved, total=len(rows), folder=folder, pdf_url=pdf_url)


@app.route("/api/print_pdf/<batch_id>")
def api_print_pdf(batch_id):
    if not _valid_batch_id(batch_id):
        return "Not found", 404
    out_path = TO_PRINT_DIR / batch_id / ("%s_print.pdf" % batch_id)
    if not out_path.exists():
        return "Not found", 404
    return send_file(out_path, mimetype="application/pdf")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = _json_body()
    batch_id = data.get("batch_id")
    raw = data.get("decoded_string")
    scanned = _sanitize(raw.strip()) if isinstance(raw, str) else ""
    if not scanned:
        return jsonify(error="Empty scan"), 400
    with get_db() as db:
        # Prefer a copy not scanned yet, so a code that appears twice in the
        # batch can still have every copy marked.
        row = db.execute(
            "SELECT * FROM barcodes WHERE batch_id=? AND decoded=? "
            "ORDER BY CASE WHEN status='scanned' THEN 1 ELSE 0 END, seq_num LIMIT 1",
            (batch_id, scanned)).fetchone()
        if not row:
            return jsonify(status="unknown", message="Barcode not found in this batch")
        db.execute("UPDATE barcodes SET status='scanned', scanned_at=? WHERE id=?",
                   (_utcnow(), row["id"]))
        # Auto-complete the batch once every barcode has been scanned.
        # decode_failed rows have no decoded value and can never be scanned,
        # so a batch that has any is left for the operator to complete manually.
        left = db.execute(
            "SELECT COUNT(*) AS n FROM barcodes WHERE batch_id=? AND status!='scanned'",
            (batch_id,)).fetchone()["n"]
        batch_completed = False
        if left == 0:
            cur = db.execute(
                "UPDATE batches SET status='completed' WHERE id=? AND status='processed'",
                (batch_id,))
            batch_completed = cur.rowcount > 0
            if batch_completed:
                log.info("Batch %s: all barcodes scanned — marked completed", batch_id)
    return jsonify(status="ok", barcode_id=row["id"], decoded=scanned,
                   batch_completed=batch_completed)


@app.route("/api/reconcile/<batch_id>")
def api_reconcile(batch_id):
    with get_db() as db:
        rows = db.execute("SELECT * FROM barcodes WHERE batch_id=? AND decoded IS NOT NULL",
                          (batch_id,)).fetchall()
    scanned = [dict(r) for r in rows if r["status"] == "scanned"]
    missed = [dict(r) for r in rows if r["status"] != "scanned"]
    return jsonify(total=len(rows), scanned=len(scanned), missed=missed,
                   reprint_needed=len(missed))


@app.route("/api/reprint", methods=["POST"])
def api_reprint():
    batch_id = _json_body().get("batch_id")
    with get_db() as db:
        if not _batch_exists(db, batch_id):
            return jsonify(error="Batch not found"), 404
        reprint_batch_dir = TO_REPRINT_DIR / batch_id
        if reprint_batch_dir.exists():
            shutil.rmtree(reprint_batch_dir, ignore_errors=True)
        reprint_batch_dir.mkdir(exist_ok=True)
        rows = db.execute(
            "SELECT * FROM barcodes WHERE batch_id=? AND status!='scanned' AND decoded IS NOT NULL ORDER BY seq_num",
            (batch_id,)).fetchall()
        rows = [dict(r) for r in rows]
        saved = 0
        folder = pdf_url = None
        if rows:
            out_path = reprint_batch_dir / ("%s_reprint.pdf" % batch_id)
            try:
                save_barcodes_as_pdf(rows, out_path)
                saved = len(rows)
                folder = str(reprint_batch_dir.resolve())
                pdf_url = url_for("api_reprint_pdf", batch_id=batch_id)
                for row in rows:
                    db.execute("UPDATE barcodes SET status='reprinted' WHERE id=?", (row["id"],))
            except Exception as e:
                log.error("PDF reprint save failed: %s", e)
    return jsonify(reprinted=saved, total=len(rows), folder=folder, pdf_url=pdf_url)


@app.route("/api/reprint_pdf/<batch_id>")
def api_reprint_pdf(batch_id):
    if not _valid_batch_id(batch_id):
        return "Not found", 404
    out_path = TO_REPRINT_DIR / batch_id / ("%s_reprint.pdf" % batch_id)
    if not out_path.exists():
        return "Not found", 404
    return send_file(out_path, mimetype="application/pdf")


@app.route("/api/batch/<batch_id>/complete", methods=["POST"])
def api_complete_batch(batch_id):
    with get_db() as db:
        if not _batch_exists(db, batch_id):
            return jsonify(error="Batch not found"), 404
        db.execute("UPDATE batches SET status='completed' WHERE id=?", (batch_id,))
    return jsonify(status="ok")


@app.route("/api/batch/<batch_id>", methods=["DELETE"])
def api_delete_batch(batch_id):
    # batch_id goes into rmtree paths: reject anything that isn't a real,
    # well-formed batch ID (e.g. "..", which would point at /data itself).
    with get_db() as db:
        if not _batch_exists(db, batch_id):
            return jsonify(error="Batch not found"), 404
        rows = db.execute("SELECT img_path FROM barcodes WHERE batch_id=?", (batch_id,)).fetchall()
        for row in rows:
            if row["img_path"]:
                try:
                    Path(row["img_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
        db.execute("DELETE FROM barcodes WHERE batch_id=?", (batch_id,))
        db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
    for d in (BARCODE_IMG_DIR / batch_id, TO_PRINT_DIR / batch_id, TO_REPRINT_DIR / batch_id):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    for ext in (".pdf", ".eps"):
        (UPLOAD_DIR / (batch_id + ext)).unlink(missing_ok=True)
    return jsonify(status="ok")


# ---------------------------------------------------------------------------
# Static assets served as plain text — no Jinja, no escaping issues
# ---------------------------------------------------------------------------
COMMON_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #f5f5f5; --surface: #fff; --text: #333; --text-muted: #666;
  --heading: #1a1a2e; --heading2: #16213e;
  --border: #eee; --input-border: #ddd;
  --th-bg: #f8f8f8; --stat-bg: #e8f4f8;
  --stat-num: #0f3460; --btn-bg: #0f3460; --btn-hover: #16213e;
  --flash-ok-bg: #d5f5e3; --flash-ok-text: #1e8449;
  --flash-err-bg: #fadbd8; --flash-err-text: #922b21;
  --flash-warn-bg: #fdebd0; --flash-warn-text: #9a4a00;
  --shadow: rgba(0,0,0,0.1);
}
body.dark {
  --bg: #1a1a2e; --surface: #16213e; --text: #e0e0e0; --text-muted: #999;
  --heading: #e0e0e0; --heading2: #c0c0d0;
  --border: #2a2a4a; --input-border: #3a3a5a;
  --th-bg: #1e1e38; --stat-bg: #1e2a4a;
  --stat-num: #7eb8f7; --btn-bg: #0f3460; --btn-hover: #1a4a80;
  --flash-ok-bg: #1a3a2a; --flash-ok-text: #4caf80;
  --flash-err-bg: #3a1a1a; --flash-err-text: #e57373;
  --flash-warn-bg: #3a2a12; --flash-warn-text: #f0a04b;
  --shadow: rgba(0,0,0,0.4);
}

body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); padding: 20px; transition: background 0.2s, color 0.2s; }
.container { max-width: 960px; margin: 0 auto; }
.topbar { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-bottom: 16px; }
.topbar-switch { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-muted); cursor: pointer; }
.toggle { position: relative; display: inline-block; width: 36px; height: 20px; }
.toggle input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; inset: 0; background: #ccc; border-radius: 20px; transition: 0.2s; }
.slider:before { content: ''; position: absolute; width: 14px; height: 14px; left: 3px; bottom: 3px;
                  background: white; border-radius: 50%; transition: 0.2s; }
input:checked + .slider { background: #0f3460; }
input:checked + .slider:before { transform: translateX(16px); }
h1 { margin-bottom: 20px; color: var(--heading); }
h2 { margin: 20px 0 10px; color: var(--heading2); }
.card { background: var(--surface); border-radius: 8px; padding: 20px;
        margin-bottom: 16px; box-shadow: 0 2px 4px var(--shadow); }
button, .btn { background: var(--btn-bg); color: #fff; border: none;
       padding: 10px 20px; border-radius: 6px; cursor: pointer;
       font-size: 14px; margin: 4px; }
button:hover { background: var(--btn-hover); }
.btn-danger { background: #c0392b; }
.btn-success { background: #27ae60; }
input[type=file], input[type=text], select {
  padding: 8px 12px; border: 1px solid var(--input-border); border-radius: 6px;
  font-size: 14px; margin: 4px; background: var(--surface); color: var(--text); }
table { width: 100%; border-collapse: collapse; margin: 10px 0; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { background: var(--th-bg); font-weight: 600; white-space: nowrap; }
td { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td button { padding: 6px 10px; font-size: 12px; margin: 2px; display: block; width: 100%; }
td.decoded { white-space: normal; word-break: break-all; overflow: visible; }
.status-decoded { color: #2980b9; }
.status-printed { color: #8e44ad; }
.status-scanned { color: #27ae60; }
.status-reprinted { color: #e67e22; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.stat { background: var(--stat-bg); padding: 12px 20px; border-radius: 8px;
        text-align: center; min-width: 100px; }
.stat .num { font-size: 28px; font-weight: bold; color: var(--stat-num); }
.stat .label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.scan-input { font-size: 20px; padding: 12px; width: 100%; }
.flash { padding: 12px; border-radius: 6px; margin: 8px 0; }
.flash-ok { background: var(--flash-ok-bg); color: var(--flash-ok-text); }
.flash-err { background: var(--flash-err-bg); color: var(--flash-err-text); }
.flash-warn { background: var(--flash-warn-bg); color: var(--flash-warn-text); }
.barcode-thumb { width: 60px; height: 60px; object-fit: contain; }
#scan-log { max-height: 400px; overflow-y: auto; }
.progress-bar { width: 100%; height: 16px; background: var(--th-bg); border-radius: 8px;
                 overflow: hidden; margin: 10px 0 4px; border: 1px solid var(--border); }
.progress-bar-fill { height: 100%; width: 0%; background: var(--btn-bg); transition: width 0.25s ease; }
.progress-label { font-size: 13px; color: var(--text-muted); }
#print-result code { word-break: break-all; background: rgba(0,0,0,0.06); padding: 1px 4px; border-radius: 3px; }
"""


@app.route("/static/style.css")
def serve_css():
    return Response(COMMON_CSS, mimetype="text/css")


@app.route("/static/station-a.js")
def serve_station_a_js():
    return Response(STATION_A_JS, mimetype="application/javascript")


@app.route("/static/station-b.js")
def serve_station_b_js():
    return Response(STATION_B_JS, mimetype="application/javascript")


# ---------------------------------------------------------------------------
# Station A HTML
# ---------------------------------------------------------------------------
STATION_A_HTML = """<!DOCTYPE html>
<html><head><title>Printer station</title><link rel="stylesheet" href="/static/style.css"></head>
<body><div class="container">
<div class="topbar">
  <span class="topbar-switch">
    EN
    <label class="toggle"><input type="checkbox" id="langToggle"><span class="slider"></span></label>
    RU
  </span>
  <span class="topbar-switch">
    <label class="toggle"><input type="checkbox" id="darkToggle"><span class="slider"></span></label>
    <span data-i18n="dark_mode">Dark mode</span>
  </span>
</div>
<h1 data-i18n="title_a">Printer station</h1>
<div class="card"><h2 data-i18n="upload_title">Upload PDF</h2>
  <input type="file" id="fileInput" accept=".pdf">
  <button id="btnUpload" data-i18n="btn_upload">Upload and process</button>
  <div id="upload-status"></div>
  <div id="upload-progress-wrap" style="display:none">
    <div class="progress-bar"><div class="progress-bar-fill" id="uploadProgressFill"></div></div>
    <div id="upload-progress-label" class="progress-label"></div>
  </div>
</div>
<div class="card"><h2 data-i18n="batches_title">Batches</h2>
  <button id="btnRefresh" data-i18n="btn_refresh">Refresh</button>
  <div id="batches"></div>
</div>
<div class="card" id="detail-card" style="display:none">
  <h2><span data-i18n="batch_detail">Batch detail</span>: <span id="detail-batch-id"></span></h2>
  <div class="stats" id="detail-stats"></div>
  <button id="btnPrint" data-i18n="btn_print">Save to PDF file</button>
  <button id="btnReconcile" class="btn-success" data-i18n="btn_reconcile">Reconcile</button>
  <button id="btnReprint" class="btn-danger" data-i18n="btn_reprint">PDF file to reprint</button>
  <div id="print-result"></div>
  <div id="reconcile-result"></div>
  <div style="margin:10px 0"><label>
    <input type="checkbox" id="hideScanned"> <span data-i18n="hide_scanned">Hide scanned</span>
  </label></div>
  <table>
    <colgroup>
      <col style="width:40px">
      <col style="width:70px">
      <col style="width:130px">
      <col>
      <col style="width:110px">
    </colgroup>
    <thead><tr>
      <th data-i18n="col_num">#</th>
      <th data-i18n="col_image">Image</th>
      <th data-i18n="col_id">ID</th>
      <th data-i18n="col_decoded">Decoded</th>
      <th data-i18n="col_status">Status</th>
    </tr></thead>
    <tbody id="detail-table"></tbody>
  </table>
</div>
<script src="/static/common.js"></script>
<script src="/static/station-a.js"></script>
</div></body></html>"""


@app.route("/printer-station")
def page_a():
    return Response(STATION_A_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# Station B HTML
# ---------------------------------------------------------------------------
STATION_B_HTML = """<!DOCTYPE html>
<html><head><title>Scanner station</title><link rel="stylesheet" href="/static/style.css"></head>
<body><div class="container">
<div class="topbar">
  <span class="topbar-switch">
    EN
    <label class="toggle"><input type="checkbox" id="langToggle"><span class="slider"></span></label>
    RU
  </span>
  <span class="topbar-switch">
    <label class="toggle"><input type="checkbox" id="darkToggle"><span class="slider"></span></label>
    <span data-i18n="dark_mode">Dark mode</span>
  </span>
</div>
<h1 data-i18n="title_b">Scanner station</h1>
<div class="card"><h2 data-i18n="select_batch">Select batch</h2>
  <select id="batchSelect"><option value="" data-i18n="select_placeholder">-- select --</option></select>
  <button id="btnRefreshB" data-i18n="btn_refresh">Refresh</button>
  <div class="stats" id="b-stats"></div>
</div>
<div class="card"><h2 data-i18n="scan_title">Scan barcode</h2>
  <p data-i18n="scan_hint">Focus the input field and scan with the wireless scanner:</p>
  <input type="text" id="scanInput" class="scan-input" data-i18n-placeholder="scan_placeholder">
  <button id="btnScan" class="btn-success" data-i18n="btn_scan">Submit</button>
  <div id="scan-feedback"></div>
</div>
<div class="card"><h2 data-i18n="scan_log">Scan log</h2><div id="scan-log"></div></div>
<div class="card"><h2 data-i18n="reconcile_title">Reconciliation</h2>
  <button id="btnReconB" data-i18n="btn_check_missing">Check missing</button>
  <div id="recon-result"></div>
</div>
<script src="/static/common.js"></script>
<script src="/static/station-b.js"></script>
</div></body></html>"""


@app.route("/scanner-station")
def page_b():
    return Response(STATION_B_HTML, mimetype="text/html")
    

COMMON_JS = r"""
var TRANSLATIONS = {
  en: {
    title_a: "Printer station", title_b: "Scanner station",
    dark_mode: "Dark mode",
    upload_title: "Upload PDF", btn_upload: "Upload and process",
    batches_title: "Batches", btn_refresh: "Refresh",
    batch_detail: "Batch detail",
    btn_print: "Save to PDF file", btn_reconcile: "Reconcile",
    btn_reprint: "PDF file to reprint",
    hide_scanned: "Hide scanned",
    col_num: "#", col_image: "Image", col_id: "ID",
    col_decoded: "Decoded", col_status: "Status", col_file: "File name", col_created: "Uploaded and processed",
    select_batch: "Select batch", select_placeholder: "-- select --",
    scan_title: "Scan barcode",
    scan_hint: "Click the field below and scan:",
    scan_placeholder: "Click me and scan...",
    btn_scan: "Submit", scan_log: "Scan log",
    reconcile_title: "Reconciliation", btn_check_missing: "Check missing",
    stat_total: "Total", stat_scanned: "Scanned", stat_missing: "Missing",
    stat_scanned_ok: "Scanned OK", stat_missed: "Missed",
    status_to_be_scanned: "To be scanned",
    lbl_missing: "Missing (not scanned):",
    all_scanned: "All barcodes scanned!",
    batch_auto_completed: "All barcodes scanned — batch marked as completed.",
    processing: "Processing...",
    pages_label: "pages", upload_failed: "Upload failed — could not read the PDF",
    saved_to: "Saved to:", open_pdf: "Open PDF", reprint_saved_to: "Reprint saved to:",
    nothing_to_print: "No decoded barcodes to save",
    complete: "Mark batch as completed?",
    delete_confirm: "Delete batch and all its data?",
    btn_view: "View", btn_complete: "Complete", btn_delete: "Delete",
    err_not_found: "Barcode not found in this batch",
    err_network: "Server not reachable — try again",
    upload_done: "{id}: {decoded} decoded, {failed} failed",
    dup_in_batch: "{n} code(s) appear more than once in this batch",
    dup_other: "{n} code(s) already exist in: {batches}",
    dup_title: "Duplicate codes — check before printing",
  },
  ru: {
    title_a: "Станция печати", title_b: "Станция сканирования",
    dark_mode: "Тёмная тема",
    upload_title: "Загрузка PDF", btn_upload: "Загрузить и обработать",
    batches_title: "Партии", btn_refresh: "Обновить",
    batch_detail: "Детали партии",
    btn_print: "Сохранить в PDF", btn_reconcile: "Сверить",
    btn_reprint: "PDF для допечати",
    hide_scanned: "Скрыть отсканированные",
    col_num: "№", col_image: "Изображение", col_id: "ID",
    col_decoded: "Декодировано", col_status: "Статус", col_file: "Имя файла", col_created: "Загружено и обработано",
    select_batch: "Выбор партии", select_placeholder: "-- выбрать --",
    scan_title: "Сканирование",
    scan_hint: "Нажмите курсором в поле ниже и сканируйте:",
    scan_placeholder: "Нажми на меня и сканируй...",
    btn_scan: "Отправить", scan_log: "Журнал сканирования",
    reconcile_title: "Сверка", btn_check_missing: "Найти пропущенные",
    stat_total: "Всего", stat_scanned: "Отсканировано", stat_missing: "Пропущено",
    stat_scanned_ok: "Отсканировано", stat_missed: "Пропущено",
    status_to_be_scanned: "Ожидает сканирования",
    lbl_missing: "Пропущенные (не отсканированы):",
    all_scanned: "Все штрихкоды отсканированы!",
    batch_auto_completed: "Все штрихкоды отсканированы — партия отмечена завершённой.",
    processing: "Обработка...",
    pages_label: "стр.", upload_failed: "Ошибка загрузки — не удалось прочитать PDF",
    saved_to: "Сохранено в:", open_pdf: "Открыть PDF", reprint_saved_to: "Файл для допечати сохранён в:",
    nothing_to_print: "Нет декодированных штрихкодов для сохранения",
    complete: "Отметить партию как завершённую?",
    delete_confirm: "Удалить партию и все данные?",
    btn_view: "Показать партию", btn_complete: "Отметить завершенной", btn_delete: "Удалить партию",
    err_not_found: "Штрихкод не найден в этой партии",
    err_network: "Сервер недоступен — повторите",
    upload_done: "{id}: декодировано {decoded}, ошибок {failed}",
    dup_in_batch: "Кодов, повторяющихся в этой партии: {n}",
    dup_other: "Кодов, уже существующих в других партиях: {n} ({batches})",
    dup_title: "Повторяющиеся коды — проверьте перед печатью",
  }
};

var pageKey = window.location.pathname.replace(/\//g, "_");
var lang = localStorage.getItem("lang_" + pageKey) || "en";
var dark = localStorage.getItem("dark_" + pageKey) === "1";

function t(key, vars) {
  var s = (TRANSLATIONS[lang] && TRANSLATIONS[lang][key]) || key;
  if (vars) {
    for (var k in vars) s = s.split("{" + k + "}").join(String(vars[k]));
  }
  return s;
}

// Escape any value from the server or user input before putting it into
// innerHTML, so it is always shown as text and never parsed as HTML.
function escHtml(v) {
  return String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Duplicate-code warning for a batch, as HTML ("" if none).
function dupWarning(b) {
  var parts = [];
  if (b.dup_in_batch) parts.push(escHtml(t("dup_in_batch", {n: b.dup_in_batch})));
  if (b.dup_other) parts.push(escHtml(t("dup_other", {n: b.dup_other, batches: (b.dup_batches || "").split(",").join(", ")})));
  if (!parts.length) return "";
  return '<div class="flash flash-warn"><strong>' + escHtml(t("dup_title")) + "</strong><br>" + parts.join("<br>") + "</div>";
}

function applyLang() {
  document.querySelectorAll("[data-i18n]").forEach(function(el) {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(function(el) {
    el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
  });
}

document.getElementById("langToggle").addEventListener("change", function() {
    lang = this.checked ? "ru" : "en";
    localStorage.setItem("lang_" + pageKey, lang);
    applyLang();
    if (typeof onLangChange === "function") onLangChange();  // hook for page JS
});

function applyDark() {
  document.body.classList.toggle("dark", dark);
  var tog = document.getElementById("darkToggle");
  if (tog) tog.checked = dark;
}

function applyLangToggle() {
  var tog = document.getElementById("langToggle");
  if (tog) tog.checked = (lang === "ru");
}

document.addEventListener("DOMContentLoaded", function() {
  applyDark();
  applyLang();
  applyLangToggle();

  document.getElementById("darkToggle").addEventListener("change", function() {
    dark = this.checked;
    localStorage.setItem("dark_" + pageKey, dark ? "1" : "0");
    applyDark();
  });

  document.getElementById("langToggle").addEventListener("change", function() {
    lang = this.checked ? "ru" : "en";
    localStorage.setItem("lang_" + pageKey, lang);
    applyLang();
  });
});
"""


# ---------------------------------------------------------------------------
# Station A JavaScript
# ---------------------------------------------------------------------------
STATION_A_JS = r"""
var currentBatch = null;
var currentBarcodes = [];

function displayStatus(s) {
    if (s === "decoded") return t("status_to_be_scanned");
    return s;
}

function renderTable() {
    var hide = document.getElementById("hideScanned").checked;
    var html = "";
    for (var i = 0; i < currentBarcodes.length; i++) {
        var b = currentBarcodes[i];
        if (hide && b.status === "scanned") continue;
        var st = displayStatus(b.status);
        html += "<tr><td><strong>" + escHtml(b.seq_num || "") + "</strong></td>";
        html += '<td><img class="barcode-thumb" src="/api/barcode_image/' + encodeURIComponent(b.id) + '"></td>';
        html += "<td>" + escHtml(b.id) + "</td>";
        html += '<td class="decoded">' + escHtml(b.decoded || "") + "</td>";
        html += '<td class="status-' + escHtml(b.status) + '">' + escHtml(st) + "</td></tr>";
    }
    document.getElementById("detail-table").innerHTML = html;
}

var uploadPollTimer = null;

function setUploadProgress(done, total) {
    var pct = total > 0 ? Math.round((done / total) * 100) : 0;
    document.getElementById("uploadProgressFill").style.width = pct + "%";
    document.getElementById("upload-progress-label").innerText = done + " / " + total + " " + t("pages_label");
}

function pollUploadProgress(batchId) {
    if (uploadPollTimer) clearInterval(uploadPollTimer);
    uploadPollTimer = setInterval(function() {
        fetch("/api/upload_progress/" + encodeURIComponent(batchId)).then(function(r) { return r.json(); }).then(function(d) {
            setUploadProgress(d.pages_done, d.total_pages);
            if (d.status === "processed" || d.status === "error" || d.error) {
                clearInterval(uploadPollTimer);
                uploadPollTimer = null;
                document.getElementById("btnUpload").disabled = false;
                if (d.status !== "processed") {
                    document.getElementById("upload-status").innerHTML =
                        '<div class="flash flash-err">' + escHtml(t("upload_failed")) + '</div>';
                } else {
                    document.getElementById("upload-status").innerHTML =
                        '<div class="flash flash-ok">' +
                        escHtml(t("upload_done", {id: batchId, decoded: d.decoded, failed: d.failed})) + "</div>" +
                        dupWarning(d);
                }
                setTimeout(function() {
                    document.getElementById("upload-progress-wrap").style.display = "none";
                }, 1500);
                loadBatches();
            }
        });
    }, 600);
}

function upload() {
    var file = document.getElementById("fileInput").files[0];
    if (!file) { alert("Select a file"); return; }
    var fd = new FormData();
    fd.append("file", file);
    document.getElementById("upload-status").innerHTML = '<div class="flash flash-ok">' + t("processing") + '</div>';
    document.getElementById("upload-progress-wrap").style.display = "block";
    setUploadProgress(0, 0);
    document.getElementById("btnUpload").disabled = true;
    function fail(msg) {
        document.getElementById("upload-status").innerHTML = '<div class="flash flash-err">' + escHtml(msg) + '</div>';
        document.getElementById("upload-progress-wrap").style.display = "none";
        document.getElementById("btnUpload").disabled = false;
    }
    fetch("/api/upload", {method: "POST", body: fd}).then(function(r) {
        return r.json();
    }).then(function(d) {
        if (d.error) { fail(d.error); return; }
        pollUploadProgress(d.batch_id);
    }).catch(function() { fail(t("err_network")); });
}

function loadBatches() {
    fetch("/api/batches").then(function(r) { return r.json(); }).then(function(batches) {
        var html = "<table><colgroup><col style='width:90px'><col><col style='width:90px'><col style='width:150px'><col style='width:260px'></colgroup><thead><tr>";
        html += '<th>ID</th><th>' + t("col_file") + '</th>';
        html += '<th>' + t("col_status") + '</th><th>' + t("col_created") + '</th>';
        html += '<th></th></tr></thead><tbody>';
        for (var i = 0; i < batches.length; i++) {
            var b = batches[i];
            var id = escHtml(b.id);
            html += "<tr><td>" + id + "</td>";
            html += "<td style='word-break:break-all;white-space:normal'>" + escHtml(b.filename) + dupWarning(b) + "</td>";
            html += "<td>" + escHtml(b.status) + "</td>";
            html += "<td>" + escHtml((b.created_at || "").slice(0, 16)) + "</td>";
            html += "<td>";
            html += "<td style='white-space:nowrap'>";
            html += '<button data-action="view" data-id="' + id + '">' + escHtml(t("btn_view")) + '</button>';
            html += '<button data-action="complete" data-id="' + id + '" class="btn-success">' + escHtml(t("btn_complete")) + '</button>';
            html += '<button data-action="delete" data-id="' + id + '" class="btn-danger">' + escHtml(t("btn_delete")) + '</button>';
            html += "</td></tr>";
        }
        html += "</tbody></table>";
        document.getElementById("batches").innerHTML = html;
    });
}

function loadBatch(bid) {
    currentBatch = bid;
    document.getElementById("detail-card").style.display = "block";
    document.getElementById("detail-batch-id").textContent = bid;
    document.getElementById("reconcile-result").innerHTML = "";
    fetch("/api/barcodes/" + bid).then(function(r) { return r.json(); }).then(function(barcodes) {
        currentBarcodes = barcodes;
        var counts = {};
        for (var i = 0; i < barcodes.length; i++) {
            var st = displayStatus(barcodes[i].status);
            counts[st] = (counts[st] || 0) + 1;
        }
        var statsHtml = "";
        for (var s in counts) {
            statsHtml += '<div class="stat"><div class="num">' + counts[s] + '</div><div class="label">' + escHtml(s) + "</div></div>";
        }
        document.getElementById("detail-stats").innerHTML = statsHtml;
        renderTable();
    });
}

function saveToPdf() {
    if (!currentBatch) return;
    fetch("/api/print", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({batch_id: currentBatch, barcode_ids: "all"})
    }).then(function(r) { return r.json(); }).then(function(d) {
        var box = document.getElementById("print-result");
        if (d.folder) {
            box.innerHTML = '<div class="flash flash-ok">' + escHtml(t("saved_to")) + ' <code>' + escHtml(d.folder) + '</code>' +
                (d.pdf_url ? ' &nbsp;<a href="' + escHtml(d.pdf_url) + '" target="_blank">' + escHtml(t("open_pdf")) + '</a>' : '') +
                '</div>';
        } else {
            box.innerHTML = '<div class="flash flash-err">' + t("nothing_to_print") + '</div>';
        }
        loadBatch(currentBatch);
    });
}

function doReconcile() {
    if (!currentBatch) return;
    fetch("/api/barcodes/" + currentBatch).then(function(r) { return r.json(); }).then(function(barcodes) {
        currentBarcodes = barcodes;
        var counts = {};
        for (var i = 0; i < barcodes.length; i++) {
            var st = displayStatus(barcodes[i].status);
            counts[st] = (counts[st] || 0) + 1;
        }
        var statsHtml = "";
        for (var s in counts) {
            statsHtml += '<div class="stat"><div class="num">' + counts[s] + '</div><div class="label">' + escHtml(s) + "</div></div>";
        }
        document.getElementById("detail-stats").innerHTML = statsHtml;
        renderTable();
    });
    fetch("/api/reconcile/" + currentBatch).then(function(r) { return r.json(); }).then(function(d) {
        var html = '<div class="stats">';
        html += '<div class="stat"><div class="num">' + d.scanned + '</div><div class="label">' + t("stat_scanned_ok") + '</div></div>';
        html += '<div class="stat"><div class="num">' + d.missed.length + '</div><div class="label">' + t("stat_missed") + '</div></div>';
        html += "</div>";
        document.getElementById("reconcile-result").innerHTML = html;
    });
}

function reprintMissed() {
    if (!currentBatch) return;
    fetch("/api/reprint", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({batch_id: currentBatch})
    }).then(function(r) { return r.json(); }).then(function(d) {
        var box = document.getElementById("print-result");
        if (d.folder) {
            box.innerHTML = '<div class="flash flash-ok">' + escHtml(t("reprint_saved_to")) + ' <code>' + escHtml(d.folder) + '</code>' +
                (d.pdf_url ? ' &nbsp;<a href="' + escHtml(d.pdf_url) + '" target="_blank">' + escHtml(t("open_pdf")) + '</a>' : '') +
                '</div>';
        } else {
            box.innerHTML = '<div class="flash flash-err">' + t("nothing_to_print") + '</div>';
        }
        loadBatch(currentBatch);
    });
}

function completeBatch(bid) {
    if (!confirm(t("complete"))) return;
    fetch("/api/batch/" + bid + "/complete", {method: "POST"}).then(function() {
        loadBatches();
        if (currentBatch === bid) {
            document.getElementById("detail-card").style.display = "none";
            currentBatch = null;
        }
    });
}

function deleteBatch(bid) {
    if (!confirm(t("delete_confirm"))) return;
    fetch("/api/batch/" + bid, {method: "DELETE"}).then(function() {
        loadBatches();
        if (currentBatch === bid) {
            document.getElementById("detail-card").style.display = "none";
            currentBatch = null;
        }
    });
}

document.getElementById("btnUpload").addEventListener("click", upload);
document.getElementById("btnRefresh").addEventListener("click", loadBatches);
document.getElementById("btnPrint").addEventListener("click", saveToPdf);
document.getElementById("btnReconcile").addEventListener("click", doReconcile);
document.getElementById("btnReprint").addEventListener("click", reprintMissed);
document.getElementById("hideScanned").addEventListener("change", renderTable);

document.getElementById("batches").addEventListener("click", function(e) {
    var btn = e.target.closest("button[data-action]");
    if (!btn) return;
    var action = btn.getAttribute("data-action");
    var id = btn.getAttribute("data-id");
    if (action === "view") loadBatch(id);
    else if (action === "complete") completeBatch(id);
    else if (action === "delete") deleteBatch(id);
});

loadBatches();
function onLangChange() { loadBatches(); }
"""


# ---------------------------------------------------------------------------
# Station B JavaScript
# ---------------------------------------------------------------------------
STATION_B_JS = r"""
var batchId = null;
var scanLog = [];

function loadBatchList() {
    fetch("/api/batches").then(function(r) { return r.json(); }).then(function(all) {
        var sel = document.getElementById("batchSelect");
        var html = '<option value="">' + escHtml(t("select_placeholder")) + '</option>';
        for (var i = 0; i < all.length; i++) {
            var b = all[i];
            if (b.status !== "processed") continue;
            var fname = b.filename || "";
            var name = fname.length > 30 ? fname.slice(0, 30) + "..." : fname;
            var date = (b.created_at || "").slice(0, 16).replace("T", " ");
            html += '<option value="' + escHtml(b.id) + '">' + escHtml(b.id + " - " + name + " - " + date) + "</option>";
        }
        sel.innerHTML = html;
    });
}

function refreshStats() {
    if (!batchId) return;
    fetch("/api/reconcile/" + batchId).then(function(r) { return r.json(); }).then(function(d) {
        document.getElementById("b-stats").innerHTML =
            '<div class="stat"><div class="num">' + d.total + '</div><div class="label">' + t("stat_total") + '</div></div>' +
            '<div class="stat"><div class="num">' + d.scanned + '</div><div class="label">' + t("stat_scanned") + '</div></div>' +
            '<div class="stat"><div class="num">' + d.missed.length + '</div><div class="label">' + t("stat_missing") + '</div></div>';
    });
}

function submitScan() {
    var inp = document.getElementById("scanInput");
    var val = inp.value.trim();
    if (!val || !batchId) return;
    fetch("/api/scan", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({batch_id: batchId, decoded_string: val})
    }).then(function(r) { return r.json(); }).then(function(d) {
        var time = new Date().toLocaleTimeString();
        var cls = d.status === "ok" ? "flash-ok" : "flash-err";
        var errMsg = d.status === "unknown" ? t("err_not_found") : (d.message || d.error);
        var msg = d.status === "ok" ? ("OK " + time + " - " + val.slice(0, 40)) : ("WARN " + time + " - " + errMsg);
        scanLog.unshift('<div class="flash ' + cls + '">' + escHtml(msg) + "</div>");
        document.getElementById("scan-log").innerHTML = scanLog.join("");
        var feedback = '<div class="flash ' + cls + '">' + escHtml(msg) + "</div>";
        if (d.batch_completed) {
            feedback += '<div class="flash flash-ok">' + escHtml(t("batch_auto_completed")) + "</div>";
        }
        document.getElementById("scan-feedback").innerHTML = feedback;
        inp.value = "";
        inp.focus();
        refreshStats();
        if (d.batch_completed) {
            // Batch drops out of the list; keep batchId so stats stay visible.
            loadBatchList();
        }
    }).catch(function() {
        // Keep the scanned value in the field so it can be resubmitted.
        var msg = "WARN " + new Date().toLocaleTimeString() + " - " + t("err_network");
        document.getElementById("scan-feedback").innerHTML = '<div class="flash flash-err">' + escHtml(msg) + "</div>";
        inp.focus();
    });
}

function reconcileB() {
    if (!batchId) return;
    fetch("/api/reconcile/" + batchId).then(function(r) { return r.json(); }).then(function(d) {
        var html = '<div class="stats">';
        html += '<div class="stat"><div class="num">' + d.total + '</div><div class="label">' + t("stat_total") + '</div></div>';
        html += '<div class="stat"><div class="num">' + d.scanned + '</div><div class="label">' + t("stat_scanned_ok") + '</div></div>';
        html += '<div class="stat"><div class="num">' + d.missed.length + '</div><div class="label">' + t("stat_missing") + '</div></div>';
        html += "</div>";
        if (d.missed.length) {
            html += "<h3>" + t("lbl_missing") + "</h3>";
            html += "<table><colgroup><col style='width:40px'><col style='width:130px'><col></colgroup>";
            html += "<thead><tr><th>" + t("col_num") + "</th><th>ID</th><th>" + t("col_decoded") + "</th></tr></thead><tbody>";
            for (var i = 0; i < d.missed.length; i++) {
                var b = d.missed[i];
                html += "<tr><td><strong>" + escHtml(b.seq_num || "") + "</strong></td><td>" + escHtml(b.id) + "</td><td style='word-break:break-all;white-space:normal'>" + escHtml(b.decoded || "") + "</td></tr>";
            }
            html += "</tbody></table>";
        } else {
            html += '<div class="flash flash-ok">' + t("all_scanned") + '</div>';
        }
        document.getElementById("recon-result").innerHTML = html;
    });
}

document.getElementById("batchSelect").addEventListener("change", function() {
    batchId = this.value;
    if (batchId) refreshStats();
});
document.getElementById("btnRefreshB").addEventListener("click", loadBatchList);
document.getElementById("btnScan").addEventListener("click", submitScan);
document.getElementById("btnReconB").addEventListener("click", reconcileB);
document.getElementById("scanInput").addEventListener("keydown", function(e) {
    if (e.key === "Enter") submitScan();
});

loadBatchList();
document.getElementById("scanInput").focus();
function onLangChange() { loadBatchList(); }
"""


@app.route("/")
def index():
    return redirect(url_for("page_a"))


@app.route("/static/common.js")
def serve_common_js():
    return Response(COMMON_JS, mimetype="application/javascript")


if __name__ == "__main__":
    init_db()
    log.info("Starting Barcode Management Server...")
    log.info("Printer Station: http://localhost:5000/printer-station")
    log.info("Scanner Station: http://<this-ip>:5000/scanner-station")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)