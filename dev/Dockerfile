ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG DEV_ID
LABEL org.opencontainers.image.source="https://github.com/compiler-team-ru/pyasc-images" \
      io.pyasc.dev.base="${BASE_IMAGE}" io.pyasc.dev.id="${DEV_ID}"
COPY requirements.lock /opt/dev-requirements.lock
COPY wheels/ /tmp/dev-wheels/
RUN python3 -m pip freeze --all > /tmp/base-constraints.txt \
 && python3 -m pip install --no-index --find-links=/tmp/dev-wheels --require-hashes \
      -c /tmp/base-constraints.txt -r /opt/dev-requirements.lock \
 && cd /tmp/dev-wheels && sha256sum -c torch.sha256 \
 && python3 -m pip install --no-index --no-deps ./torch-*.whl \
 && python3 -m pip check \
 && rm -rf /tmp/dev-wheels /tmp/base-constraints.txt /root/.cache/pip
