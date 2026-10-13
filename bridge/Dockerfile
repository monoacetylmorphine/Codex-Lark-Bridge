# syntax=docker/dockerfile:1
FROM node:lts-bookworm-slim AS node-runtime
FROM python:3.13-slim-bookworm

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$SOURCE_REVISION

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=node-runtime /usr/local /usr/local

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/bridge/.venv/bin:${PATH}" \
    PYTHONPATH=/opt

# Source: the existing audio workflow and skills-office; explicitly install only two font sets and disable recommended packages.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg git libstdc++6 qpdf ripgrep zip unzip \
        libreoffice-writer libreoffice-calc libreoffice-impress poppler-data poppler-utils pandoc \
        fontconfig fonts-inter fonts-noto-cjk \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-jpn \
    && fc-cache -f

# Install the latest pnpm at build time; the user workspace initializer installs Office packages.
RUN --mount=type=cache,target=/root/.npm \
    npm install --global pnpm --no-audit --no-fund \
    && node --version && pnpm --version

ARG APP_USER=app
ARG APP_UID=1000
ARG APP_GID=1000
RUN if ! getent group "${APP_GID}" > /dev/null; then \
        groupadd --gid "${APP_GID}" "${APP_USER}"; \
    fi \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir "/home/${APP_USER}" --shell /bin/bash "${APP_USER}"

ENV HOME=/home/${APP_USER}
ENV CODEX_HOME=${HOME}/.codex

WORKDIR /opt/bridge
COPY bridge/pyproject.toml bridge/uv.lock ./
ARG PYTHON_PACKAGE_INDEX=https://pypi.org/simple
# 默认升级兼容依赖；CI 传入 --locked 复用本轮已测试的解析结果。
ARG UV_SYNC_FLAGS=--upgrade
# 构建缓存路径须与挂载点一致；仅对本次安装生效，运行时仍使用用户缓存。
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync ${UV_SYNC_FLAGS} --no-dev --no-install-project --python /usr/local/bin/python \
        --default-index "${PYTHON_PACKAGE_INDEX}" \
    && ln -s "$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" \
        /usr/local/bin/codex \
    && codex --version
COPY bridge/ .
# Dependencies are cached above; run the copied source via PYTHONPATH.
RUN install -m 0755 docker/entrypoint.sh /usr/local/bin/bridge-entrypoint \
    && install -d -m 0755 -o "${APP_UID}" -g "${APP_GID}" \
        "${HOME}/.codex_bridge" "${CODEX_HOME}" "${CODEX_HOME}/workspace"

USER ${APP_UID}:${APP_GID}
WORKDIR ${HOME}
ENTRYPOINT ["bridge-entrypoint"]
CMD ["python", "-m", "bridge.main"]
