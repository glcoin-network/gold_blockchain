FROM rust:1.85.1-slim-bullseye AS rust

FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_BUILD_JOBS=2 \
    GOLD_ROOT=/data/mainnet \
    GOLD_KEYS_ROOT=/data/keys

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git build-essential cmake pkg-config \
    python3 python3-dev python3-pip python3-venv \
    libgmp-dev libboost-python-dev libboost-system-dev libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:/usr/local/cargo/bin:${PATH}"
RUN python -m pip install "pip==24.3.1" "setuptools==69.5.1" "wheel==0.45.1" "maturin==1.7.8"

COPY gold_rs /opt/gold-native
RUN maturin build --release --locked \
    --manifest-path /opt/gold-native/wheel/Cargo.toml \
    --interpreter /opt/venv/bin/python --out /opt/wheels
RUN python -m pip install /opt/wheels/gold_rs-0.9.0-*.whl

COPY g-timelord/setup.py g-timelord/pyproject.toml g-timelord/README.md /opt/gold-src/
COPY g-timelord/build_scripts/timelord-recovery/build-constraints.txt /opt/gold-build-constraints.txt
WORKDIR /opt/gold-src
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_CONSTRAINT=/opt/gold-build-constraints.txt python -m pip install .
RUN --mount=type=cache,target=/root/.cache/pip \
    BUILD_VDF_BENCH=Y python -m pip install \
    --force-reinstall --no-cache-dir --no-binary=chiavdf "chiavdf==1.1.4"
COPY g-timelord /opt/gold-src
RUN chmod 644 /opt/gold-src/mozilla-ca/cacert.pem
RUN python -m pip install --no-deps --no-build-isolation --editable .
RUN python -m pip check
RUN GOLD_TEST_REAL_IMPORTS=1 python chia/_tests/timelord/test_peak_recovery.py
RUN python build_scripts/timelord-recovery/smoke_test.py

ARG GOLD_SOURCE_REVISION=unknown
ARG GOLD_NATIVE_REVISION=unknown
LABEL org.opencontainers.image.title="Gold timelord recovery - Ubuntu 20.04 amd64" \
    org.opencontainers.image.source="https://github.com/glcoin-network/gold_blockchain" \
    org.opencontainers.image.revision="${GOLD_SOURCE_REVISION}" \
    gold.native.revision="${GOLD_NATIVE_REVISION}" \
    gold.timelord.recovery="local-confirmed-ancestor-15s"

ENTRYPOINT ["gold"]
CMD ["--help"]
