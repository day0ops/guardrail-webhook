FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
RUN python -c "import nltk; nltk.download('vader_lexicon', download_dir='/usr/local/nltk_data')"

FROM python:3.13-slim

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/nltk_data /usr/local/nltk_data
COPY main.py .
COPY webhook_api.py .

ENV NLTK_DATA=/usr/local/nltk_data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/docs')" || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
