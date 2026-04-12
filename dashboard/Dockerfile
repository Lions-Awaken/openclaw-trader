FROM python:3.12-slim

WORKDIR /app

# Pinned dependencies
RUN pip install --no-cache-dir \
    fastapi==0.115.12 \
    uvicorn[standard]==0.34.2 \
    httpx==0.28.1 \
    python-multipart==0.0.20 \
    anthropic==0.88.0 \
    yfinance==0.2.54

COPY server.py index.html login.html theme.css systems-console.html ./
COPY static/ ./static/

# Non-root user
RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 8090
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
