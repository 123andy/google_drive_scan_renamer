FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# OCR toolchain (so the host needs nothing installed).
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		ocrmypdf \
		tesseract-ocr \
		ghostscript \
		qpdf \
	&& rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# One-shot batch: drain the inbox and exit.
CMD ["python", "-m", "app.main"]
