# Changelog

## 0.6.2

- Corrige a acentuação dos nomes exibidos, incluindo `Água detectada`, `Iluminância`,
  `Intervalo de iluminância`, `Conexão local`, `Inventário` e `Último evento`.
- Mantém o nome `Chuva detectada` ao aplicar o reconhecimento genérico de umidade.
- Traduz os estados Tuya `none` e `presence` para `Sem chuva` e `Chuva detectada`.

## 0.6.1

- Uniformiza DPs numericos de bateria como percentual no Home Assistant.
- Aplica `device_class: battery` e unidade `%` mesmo quando a Tuya omite esses metadados.

## 0.6.0

- Consulta os detalhes atuais de cada subdispositivo na Tuya Cloud.
- Prioriza o `custom_name` configurado no Tuya/Smart Life.
- Mantem `name`, `product_name`, modelo e Device ID como fallbacks.
- Republica o MQTT Discovery com o nome atualizado sem criar outro dispositivo.
- Inclui `custom_name` no inventario MQTT para diagnostico.

## 0.5.1

- Corrige disponibilidade presa em `offline` quando a conexao MQTT cai e reconecta.
- Republica o availability no reconnect do MQTT, no heartbeat e ao receber eventos reais do X5.
- Mantem as entidades disponiveis enquanto a conexao local com o X5 continua viva.

## 0.5.0

- Busca a especificacao completa de cada subdispositivo pela Tuya Cloud durante o auto sync.
- Marca DPs gravaveis quando aparecem em `functions` na especificacao Tuya.
- Cria controles MQTT Discovery automaticamente para DPs gravaveis:
  - `switch` para booleanos;
  - `number` para inteiros com faixa `min/max`;
  - `select` para enums com `range`;
  - `text` para strings gravaveis.
- Converte escala Tuya ao enviar comandos numericos.
- Publica `writable_dps` no inventario MQTT para diagnostico.
- Adiciona controles locais do `ZG-223Z`: sensibilidade e intervalo de iluminância.
- Remove os campos antigos `door_*` do schema de configuracao.

## 0.4.1

- Mantem as opcoes antigas `door_*` como opcionais no schema apenas para permitir a migracao.
- O `run.sh` remove automaticamente essas chaves antigas das opcoes salvas pelo Supervisor.
- Evita travamento de update local quando a configuracao antiga ainda existe.

## 0.4.0

- Remove as opcoes manuais `door_device_id`, `door_node_id` e `door_contact_dp`.
- Adiciona migracao automatica para apagar essas opcoes antigas das configuracoes salvas.
- Habilita `auto_sync` por padrao.
- Adiciona perfil local do `ZG-223Z` com rainwater, illuminance, sensitivity, illuminance_sampling e battery.
- Adiciona `repository.yaml` na pasta pai para deixar a estrutura no formato de repositorio de app.

## 0.3.1

- Reconhece sensores de chuva, água, vazamento e inundação como `binary_sensor` de umidade.
- Normaliza estados comuns como `alarm`, `wet`, `leak`, `rain`, `normal` e `dry`.

## 0.3.0

- Adiciona sincronizacao automatica de subdispositivos pela Tuya Cloud.
- Cria dispositivos MQTT Discovery dinamicamente no Home Assistant.
- Mantem compatibilidade com o Door Sensor manual e os topicos antigos.
- Cria entidades conhecidas para porta, movimento, bateria, temperatura, umidade e switches.
- Cria entidades diagnosticas para DPs desconhecidos quando habilitado.
- Adiciona topico de comandos MQTT para switches descobertos.

## 0.2.0

- Entidade binaria da porta via MQTT Discovery.
- Mapeamento configuravel do DP de contato.
- Estado e disponibilidade retidos no MQTT.
