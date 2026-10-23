ARG UBUNTU_IMAGE
ARG ENVIRONMENT_IMAGE=ubuntu:22.04
FROM ${UBUNTU_IMAGE} AS python-build
ARG PYTHON_VERSION
ARG PYTHON_URL
ARG PYTHON_SHA256
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev libffi-dev liblzma-dev libncurses-dev libgdbm-dev uuid-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fL --retry 3 "${PYTHON_URL}" -o /tmp/python.tar.xz \
    && echo "${PYTHON_SHA256}  /tmp/python.tar.xz" | sha256sum -c - \
    && mkdir /tmp/python && tar -xJf /tmp/python.tar.xz -C /tmp/python --strip-components=1 \
    && cd /tmp/python && ./configure --prefix=/opt/python --enable-shared --with-ensurepip=install LDFLAGS=-Wl,-rpath=/opt/python/lib \
    && make -j2 && make install \
    && mkdir -p /opt/python/share/licenses && cp LICENSE /opt/python/share/licenses/CPython.txt

FROM ${UBUNTU_IMAGE} AS environment
ENV PATH=/opt/python/bin:${PATH}
ENV LD_LIBRARY_PATH=/opt/python/lib
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils g++ make cmake unzip pciutils libnuma1 \
    libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
    liblzma-dev libncurses-dev libgdbm-dev libblas3 libgfortran5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-build /opt/python /opt/python
COPY requirements.lock /opt/requirements.lock
RUN python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /opt/requirements.lock \
    && python3 -m pip check
ARG CANN_URL
ARG CANN_SHA256
ARG CANN_INSTALL_ARGS
RUN curl -fL --retry 3 -H 'Referer: https://www.hiascend.com/' "${CANN_URL}" -o /tmp/cann.run \
    && echo "${CANN_SHA256}  /tmp/cann.run" | sha256sum -c - \
    && bash /tmp/cann.run ${CANN_INSTALL_ARGS} \
    && rm -f /tmp/cann.run && rm -rf /root/.cache/pip
COPY entrypoint.sh /usr/local/bin/pyasc-entrypoint
RUN chmod 755 /usr/local/bin/pyasc-entrypoint
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/pyasc-entrypoint"]
CMD ["bash"]

FROM ${ENVIRONMENT_IMAGE} AS simulator
ARG SOURCE_SHA
ARG WHEEL_VERSION
ARG RECIPE_SHA
ARG ENVIRONMENT_DIGEST
LABEL org.opencontainers.image.source="https://github.com/compiler-team-ru/pyasc-images" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      org.opencontainers.image.version="${WHEEL_VERSION}" \
      io.pyasc.recipe="${RECIPE_SHA}" \
      io.pyasc.environment="${ENVIRONMENT_DIGEST}"
COPY wheel/ /tmp/wheel/
COPY cell.json /opt/pyasc-image.json
RUN cd /tmp/wheel && sha256sum -c SHA256SUMS \
    && python3 -m pip install --no-cache-dir --no-index --no-deps *.whl \
    && python3 -m pip check && rm -rf /tmp/wheel
