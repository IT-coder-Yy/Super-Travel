FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd --uid 10001 --create-home app
COPY backend/external_tools.py backend/mcp_client.py backend/search_tools.py backend/shared_tools_server.py /app/backend/
USER app
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "backend.shared_tools_server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--limit-concurrency", "32", "--no-access-log", "--no-proxy-headers"]
