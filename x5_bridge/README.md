# X5 Bridge 0.7.1

Bridge local entre o gateway Tuya X5 e o Home Assistant via MQTT.

## Cortinas

Dispositivos Tuya das categorias de cortina são publicados como uma entidade
`cover` nativa do Home Assistant. O bridge usa preferencialmente os códigos da
especificação Tuya (`control`, `percent_control`, `percent_state` e
`work_state`) e mantém uma detecção por estados locais para firmwares OEM que
não fornecem o mapeamento completo.

## Auto sync

Preencha:

- `auto_sync: true`
- `tuya_region: us-e` para Eastern America, ou `us` para Western America
- `tuya_access_id`: Access ID do projeto Tuya Developer
- `tuya_access_secret`: Access Secret do projeto Tuya Developer
- `tuya_cloud_device_id`: Device ID do X5, ou outro device ID da mesma conta Tuya

O controle e os eventos continuam locais pelo X5. A Tuya Cloud e usada apenas para atualizar o inventario.

Durante a sincronizacao, o nome do dispositivo segue esta prioridade:

1. Nome personalizado configurado no app Tuya/Smart Life (`custom_name`).
2. Nome retornado pela lista de dispositivos Tuya (`name`).
3. Nome do produto, modelo ou Device ID.

Uma alteracao de nome no app e aplicada no proximo ciclo de sincronizacao e
republicada pelo MQTT Discovery. Um nome definido manualmente no proprio Home
Assistant continua tendo prioridade visual no Home Assistant.

## Configuracao atual

Campos usados:

- `x5_ip`
- `x5_device_id`
- `x5_local_key`
- `x5_version`
- `auto_sync`
- `sync_interval_minutes`
- `tuya_region`
- `tuya_access_id`
- `tuya_access_secret`
- `tuya_cloud_device_id`
- `create_unknown_dp_entities`
- `topic_prefix`
- `debug`

Campos antigos `door_device_id`, `door_node_id` e `door_contact_dp` nao sao mais usados. Se ainda estiverem salvos no Supervisor, o add-on tenta remove-los ao iniciar.

## Reconhecimento de dispositivos

Ordem usada:

1. Mapping/specification da propria Tuya Cloud, separando DPs de status e DPs gravaveis.
2. Perfis locais de produtos conhecidos por modelo/nome, quando a Tuya retorna nomes genericos.
3. Entidades diagnosticas para DPs desconhecidos.

Perfil local incluido:

- `ZG-223Z`: rainwater, illuminance, sensitivity, illuminance_sampling e battery.

## Entidades criadas

- Gateway X5: conexao local e inventario.
- Dispositivos descobertos: ultimo evento.
- DPs reconhecidos: porta, movimento, água/chuva/vazamento, iluminância, bateria, temperatura, umidade e switches.
- DPs gravaveis reconhecidos: switches, numeros, selects e textos com comandos enviados de volta ao X5 por Tuya LAN.
- DPs desconhecidos: entidades diagnosticas, quando `create_unknown_dp_entities` estiver ativo.
