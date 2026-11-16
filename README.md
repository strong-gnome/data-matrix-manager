# Data Matrix Manager

A small self-hosted web app for tracking batches of Data Matrix barcode labels from PDF to scanned label.

You upload a PDF that contains Data Matrix codes. The app finds and decodes every code, then makes a print-ready PDF with one label per page. As labels are scanned, it checks each one off, so you can see which ones are missing and print only those again.

It was originally built for **Honest Sign** (Честный ЗНАК), Russia's mandatory product labeling system. Marking code PDFs downloaded from Honest Sign often put many codes on a page, sometimes inside a table. This app turns them into single labels for a label printer and tracks which ones have been applied. It does not connect to the Honest Sign API. It only handles the PDFs and the physical labels, and it works with any Data Matrix codes, not just Honest Sign ones.

It has two browser pages, meant for two workstations in different places:

- **Printer station** (`/printer-station`, also where `/` redirects): set up next to the thermal label printer. The operator here uploads PDFs, looks at batches, prints label PDFs, runs the reconcile and prints reprints.
- **Scanner station** (`/scanner-station`): set up where labels get applied to products. The operator here picks a batch and scans each applied label with their own handheld or wireless scanner, which must type like a keyboard. Any such scanner works, because the app strips the GS characters that some scanners leave out (see [How it works](#how-it-works)).

Both pages have an English/Russian switch and a dark mode.

## How it works

```
 Source PDF ──► upload ──► render whole page at 400 DPI (poppler)
                               │
                               ▼
                 Data Matrix detection + decode (libdmtx / OpenCV)
                 each code cropped from the 400 DPI page image
                               │
                               ▼
        SQLite: batches + barcodes (cropped PNG for each code)
                               │
             ┌─────────────────┴──────────────────┐
             ▼                                    ▼
   Printer station: label PDF          Scanner station: scan input
   (1 label per page, with a           marks each code as "scanned"
    sequence number under it)
             │                                    │
             └──────────► reconcile ◄─────────────┘
                              │
                              ▼
                 reprint PDF of the labels not scanned yet
```

- **Resolution.** Codes are not upscaled one by one. Each PDF page is rasterised at 400 DPI as a whole, and every code is cropped from that image with a small margin. So the stored PNGs, and the labels printed from them, are 400 DPI crops of the vector source.
- **Processing runs in the background.** Uploads return right away. Pages are rendered and decoded one at a time, so memory stays flat even for big PDFs, and the printer station shows a progress bar while this runs.
- **Detection.** First, each whole page goes to libdmtx, which finds the codes itself, up to 25 per page. If that finds nothing, the app falls back to an OpenCV contour search. That search takes out table lines, keeps roughly square shapes, and sizes its filters from the median shape on that page, not from fixed limits.
- **Cleaning the decoded text.** GS (`\x1D`) separators and other control characters are removed, and repeated spaces are merged into one. This happens to the decoded values and to scanner input, so the two match when compared. Many handheld scanners drop or change the GS characters in Honest Sign codes, which is why both sides are cleaned. This only affects matching. The printed label uses the cropped image of the original code, not a re-encoded one, so the GS separators and the crypto tail stay intact on the label.

### Barcode status

| Status            | Meaning                                              |
|-------------------|------------------------------------------------------|
| `decoded`         | Found and decoded; shown as "To be scanned" in the UI |
| `decode_failed`   | A code-like region was found but could not be decoded |
| `printed`         | Included in a label PDF                              |
| `scanned`         | Matched by a scan at the scanner station             |
| `reprinted`       | Included in a reprint PDF                            |

A batch moves through `processing` → `processed` → `completed`. If the PDF can't be read, it gets `error`. The scanner station lists only `processed` batches, so a completed batch disappears from that list.

A batch is marked `completed` **automatically** when the last of its barcodes is scanned. If any barcode is `decode_failed`, the batch can't be completed this way, because that code has no value to scan against. An operator then has to check it by hand and click **Complete** on the printer station.

## Quick start (Docker)

```bash
docker build -t barcode-mgr .
docker compose up -d
```

`docker-compose.yml` points to the local `barcode-mgr` image and does not build it, so run the build step first.

Then open:

- Printer station: <http://localhost:5321/printer-station>
- Scanner station: `http://<server-ip>:5321/scanner-station`

The compose file:

- maps host port **5321** to container port 5000
- mounts `./app` at `/app`, so code and `config.ini` changes take effect after a restart, without a rebuild
- mounts `./data` at `/data`, where all state lives
- limits the container to 1000 MB of memory

## Running without Docker

You need Python 3.12+, plus **poppler** (for `pdf2image`) and **libdmtx** on the system.

```bash
# Debian/Ubuntu
sudo apt-get install poppler-utils libdmtx0b

pip install -r requirements.txt
cd app
python barcode-app.py
```

The data directory is hard-coded to `/data`, so it must exist and be writable by the user running the app. The server listens on port **5000**.

## Configuration

Label layout is set in [`app/config.ini`](app/config.ini). All values are in millimetres.

```ini
[barcode]
source_barcode_size_mm = 10   # size of the codes in the source PDF (not used yet)
print_barcode_size_mm  = 20   # size of the code on the printed label
print_paper_size_mm    = 25   # side of the square label / PDF page
seq_num_gap_mm         = 1.5  # gap between the code and the sequence number under it
```

The code is centred on the label. The batch sequence number is printed under it in bold, with the font size scaled to `seq_num_gap_mm`. Restart the app after you change the config.

If `config.ini` is missing, the app uses built-in defaults, and two of them differ from the shipped file: `print_barcode_size_mm = 15` and `seq_num_gap_mm = 2.0`.

## Data layout

Everything is kept under `/data` (`./data` on the host when you use Docker):

```
data/
├── barcodes.db            # SQLite database (WAL mode)
├── original_batches/      # uploaded source files, renamed to <batch_id>.pdf (or .eps)
├── barcode_images/<id>/   # cropped PNG of each detected code
├── to_print/<id>/         # <batch_id>_print.pdf
└── to_reprint/<id>/       # <batch_id>_reprint.pdf (replaced on each reprint)
```

Batch IDs look like `batch_<n>_<YYYY-MM-DD>`. Barcode IDs look like `<batch_id>-<page>-<index>`, where page and index both start at 0 and are zero-padded to 2 and 3 digits (for example, `batch_1_2026-11-16-00-000`).

Deleting a batch in the UI removes its database rows, images, generated PDFs and the uploaded source file.

## Typical workflow

1. **Printer station: upload.** Upload the Data Matrix PDF from Honest Sign. The app renders each whole page at 400 DPI, finds every code on it, crops it from that page image, decodes it, removes the GS characters, numbers it within the batch and saves it to the database. Wait for the progress bar to finish.
2. **Printer station: print.** Open the batch with **View** and check the decoded values. Then click **Save to PDF file** and print the labels on the thermal printer. Each label has its sequence number printed under the code.
3. **Scanner station: apply and scan.** Pick the batch, click into the scan field, and scan each label after it is applied to a product. Each scan is checked against the batch. A scanner that sends Enter after each code submits it automatically.
4. **Printer station: reconcile.** Run **Reconcile** to see which codes have not been scanned yet. These are labels that were missed, damaged, or not applied. The scanner station shows the same list with **Check missing**.
5. **Printer station: reprint.** Click **PDF file to reprint** to make a PDF of only the codes that have not been scanned, and print it.
6. **Scanner station: rescan.** Apply the reprinted labels, or rescan labels that are already on products, so they are counted.
7. Repeat steps 4–6 until every code is scanned. When the last one is scanned, the batch is marked **completed** automatically. The scanner station says so, and the batch leaves its list.

**Complete** on the printer station is still there for manual closing, for example when a batch has `decode_failed` codes or you decide to stop early.

## HTTP API

The UI uses these JSON endpoints, and you can call them yourself.

| Method | Path                               | Description |
|--------|------------------------------------|-------------|
| POST   | `/api/upload`                      | Multipart `file` (`.pdf` or `.eps`, max 50 MB). Starts background processing and returns `batch_id`. |
| GET    | `/api/upload_progress/<batch_id>`  | `status`, `total_pages`, `pages_done`, `decoded`, `failed` |
| GET    | `/api/batches`                     | All batches, newest first |
| GET    | `/api/barcodes/<batch_id>`         | All barcodes in a batch |
| GET    | `/api/barcode_image/<barcode_id>`  | Cropped PNG of one code |
| POST   | `/api/print`                       | `{"batch_id", "barcode_ids": "all" \| [ids]}`: makes the label PDF and marks the codes `printed` |
| GET    | `/api/print_pdf/<batch_id>`        | Download the label PDF |
| POST   | `/api/scan`                        | `{"batch_id", "decoded_string"}`: marks the matching code `scanned`. Returns `batch_completed: true` if that scan completed the batch |
| GET    | `/api/reconcile/<batch_id>`        | Counts of total and scanned codes, plus the list of missed codes |
| POST   | `/api/reprint`                     | `{"batch_id"}`: makes the reprint PDF of codes not scanned yet |
| GET    | `/api/reprint_pdf/<batch_id>`      | Download the reprint PDF |
| POST   | `/api/batch/<batch_id>/complete`   | Mark a batch completed |
| DELETE | `/api/batch/<batch_id>`            | Delete a batch and all its files |

## Security note

The app has **no authentication**, and it runs Flask's development server with `debug=True` on `0.0.0.0`. Run it only on a trusted local network, and don't expose it to the internet.

## License

[MIT](LICENSE)
