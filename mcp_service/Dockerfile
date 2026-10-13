# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$SOURCE_REVISION

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ARG APP_USER=app
ARG APP_UID=1000
ARG APP_GID=1000
ARG PYTHON_PACKAGE_INDEX=https://pypi.org/simple
# 默认升级兼容依赖；CI 传入 --locked 复用本轮已测试的解析结果。
ARG UV_SYNC_FLAGS=--upgrade

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/mcp_service/.venv/bin:${PATH}" \
    HOME=/home/${APP_USER} \
    CODEX_HOME=/home/${APP_USER}/.codex

WORKDIR /opt/mcp_service

COPY mcp_service/pyproject.toml mcp_service/uv.lock mcp_service/README.md ./
COPY mcp_service/__init__.py mcp_service/server.py ./
COPY bridge/configs/config_default.json bridge/configs/config_schema.json bridge/configs/validation.py bridge/configs/initialization.py bridge/configs/loader.py /opt/bridge/configs/
COPY bridge/services/lark/lark_requests.py /opt/bridge/services/lark/
COPY mcp_service/configs ./configs
COPY mcp_service/services ./services
COPY bridge/docker/entrypoint.sh /usr/local/bin/bridge-entrypoint
COPY mcp_service/tools ./tools
COPY mcp_service/utils ./utils

RUN chmod 0755 /usr/local/bin/bridge-entrypoint \
    && uv sync ${UV_SYNC_FLAGS} --no-dev --no-editable --python /usr/local/bin/python \
        --default-index "${PYTHON_PACKAGE_INDEX}" \
    && if ! getent group "${APP_GID}" > /dev/null; then \
        groupadd --gid "${APP_GID}" "${APP_USER}"; \
    fi \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir "${HOME}" --shell /usr/sbin/nologin "${APP_USER}" \
    && mkdir -p "${HOME}/.codex_bridge" "${CODEX_HOME}" \
    && chown -R "${APP_UID}:${APP_GID}" "${HOME}"

USER ${APP_UID}:${APP_GID}

# MCP 服务的默认 HTTP 端口；可使用 MCP_PORT 覆盖。
EXPOSE 8000

# 默认从 ${HOME}/.codex_bridge/config.json 读取配置。
# 可挂载该目录，或使用 CONFIG_FILE 指向另一份配置文件。
ENTRYPOINT ["bridge-entrypoint"]
CMD ["python", "-m", "mcp_service.server"]
