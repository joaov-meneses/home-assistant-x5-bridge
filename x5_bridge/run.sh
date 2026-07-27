#!/usr/bin/with-contenv bashio
set -e

export MQTT_HOST="$(bashio::services mqtt 'host')"
export MQTT_PORT="$(bashio::services mqtt 'port')"
export MQTT_USERNAME="$(bashio::services mqtt 'username')"
export MQTT_PASSWORD="$(bashio::services mqtt 'password')"

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

bashio::log.info "Iniciando X5 Bridge 0.7.1..."
bashio::log.info "Gateway: ${X5_IP} | protocolo ${X5_VERSION}"
if bashio::config.true 'auto_sync'; then
  bashio::log.info "Sincronizacao automatica ativa | regiao ${TUYA_REGION} | intervalo ${SYNC_INTERVAL_MINUTES} min"
else
  bashio::log.warning "Sincronizacao automatica desativada; nenhum subdispositivo sera descoberto automaticamente"
fi

exec /opt/x5-bridge/bin/python3 /app/bridge.py
