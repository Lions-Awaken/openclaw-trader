FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx
COPY server.py index.html ./
EXPOSE 8090
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
