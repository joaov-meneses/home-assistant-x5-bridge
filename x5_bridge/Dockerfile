FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/x5-bridge \
    && /opt/x5-bridge/bin/pip install --no-cache-dir \
        tinytuya==1.20.0 \
        paho-mqtt==2.1.0

WORKDIR /app

COPY bridge.py /app/bridge.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD ["/run.sh"]
