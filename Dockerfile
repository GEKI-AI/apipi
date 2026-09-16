FROM python:3.13-slim-bookworm

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts/microvm-rootfs ./scripts/microvm-rootfs
RUN pip install --no-cache-dir . \
    && useradd --system --uid 65532 --home /nonexistent --shell /usr/sbin/nologin apipi
USER apipi
EXPOSE 8000
CMD ["apipi", "serve", "--api-only", "--host", "0.0.0.0", "--port", "8000"]
