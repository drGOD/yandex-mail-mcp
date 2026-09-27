FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Package files only. Credentials stay in the client environment, not the image.
COPY pyproject.toml README.md LICENSE CHANGELOG.md yandex_mail_mcp.py ./

RUN pip install --no-cache-dir . \
    && useradd --create-home --shell /usr/sbin/nologin --uid 10001 mcp

USER mcp
WORKDIR /home/mcp

# stdout is the MCP protocol. The process waits for a client on stdin.
ENTRYPOINT ["yandex-mail-mcp"]
