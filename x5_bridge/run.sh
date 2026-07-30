#!/usr/bin/with-contenv bashio
set -e

MQTT_HOST=""
MQTT_PORT=""
MQTT_USERNAME=""
MQTT_PASSWORD=""

# During an app or Supervisor update, the MQTT broker can already be running
# while its service registration is not available yet. Wait for the complete
# service response instead of passing an empty port to the Python process.
for attempt in $(seq 1 30); do
  MQTT_HOST="$(bashio::services mqtt 'host' 2>/dev/null || true)"
  MQTT_PORT="$(bashio::services mqtt 'port' 2>/dev/null || true)"

  if [[ -n "${MQTT_HOST}" && "${MQTT_PORT}" =~ ^[0-9]+$ ]]; then
    MQTT_USERNAME="$(bashio::services mqtt 'username' 2>/dev/null || true)"
    MQTT_PASSWORD="$(bashio::services mqtt 'password' 2>/dev/null || true)"
    break
  fi

  if (( attempt == 1 || attempt % 5 == 0 )); then
    bashio::log.warning "Servico MQTT ainda indisponivel; nova tentativa em 2 segundos..."
  fi
  sleep 2
done

if [[ -z "${MQTT_HOST}" || ! "${MQTT_PORT}" =~ ^[0-9]+$ ]]; then
  bashio::exit.nok \
    "Servico MQTT nao foi habilitado pelo Supervisor. Reinicie o Mosquitto broker e tente novamente."
fi

export MQTT_HOST
export MQTT_PORT
export MQTT_USERNAME
export MQTT_PASSWORD

export X5_IP="$(bashio::config 'x5_ip')"
export X5_DEVICE_ID="$(bashio::config 'x5_device_id')"
export X5_LOCAL_KEY="$(bashio::config 'x5_local_key')"
export X5_VERSION="$(bashio::config 'x5_version')"
export AUTO_SYNC="$(bashio::config 'auto_sync')"
export SYNC_INTERVAL_MINUTES="$(bashio::config 'sync_interval_minutes')"
export TUYA_REGION="$(bashio::config 'tuya_region')"
export TUYA_ACCESS_ID="$(bashio::config 'tuya_access_id')"
export TUYA_ACCESS_SECRET="$(bashio::config 'tuya_access_secret')"
export TUYA_CLOUD_DEVICE_ID="$(bashio::config 'tuya_cloud_device_id')"
export CREATE_UNKNOWN_DP_ENTITIES="$(bashio::config 'create_unknown_dp_entities')"
export TOPIC_PREFIX="$(bashio::config 'topic_prefix')"
export DEBUG="$(bashio::config 'debug')"

options="$(bashio::addon.options)"
for old_key in door_device_id door_node_id door_contact_dp; do
  if bashio::jq.exists "${options}" ".${old_key}"; then
    bashio::log.info "Removendo opcao antiga: ${old_key}"
    bashio::addon.option "${old_key}"
  fi
done

bashio::log.info "Iniciando X5 Bridge 0.9.0..."
bashio::log.info "Gateway: ${X5_IP} | protocolo ${X5_VERSION}"
if bashio::config.true 'auto_sync'; then
  bashio::log.info "Sincronizacao automatica ativa | regiao ${TUYA_REGION} | intervalo ${SYNC_INTERVAL_MINUTES} min"
else
  bashio::log.warning "Sincronizacao automatica desativada; nenhum subdispositivo sera descoberto automaticamente"
fi

exec /opt/x5-bridge/bin/python3 /app/bridge.py
