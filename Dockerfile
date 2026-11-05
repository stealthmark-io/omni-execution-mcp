FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE* ./
COPY omni_execution_mcp ./omni_execution_mcp

RUN pip install --no-cache-dir -e .

ENTRYPOINT ["python3", "-m", "omni_execution_mcp.server"]
