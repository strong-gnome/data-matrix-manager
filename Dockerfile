FROM python:3.12-slim

# System deps: poppler (pdf2image), libdmtx, fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libdmtx0b \
    libdmtx-dev \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

WORKDIR /app

RUN mkdir -p /data

EXPOSE 5000

CMD ["python", "barcode-app.py"]