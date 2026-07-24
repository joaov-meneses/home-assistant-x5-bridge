# X5 Bridge Add-on Repository

Repositório do add-on **X5 Bridge** para o Home Assistant.

O add-on conecta localmente um gateway Tuya X5 ao broker MQTT do Home
Assistant, publica os dispositivos por MQTT Discovery e pode usar a Tuya Cloud
somente para sincronizar o inventário e as especificações dos dispositivos.

## Instalação

1. No Home Assistant, acesse **Configurações > Add-ons > Loja de add-ons**.
2. Abra o menu no canto superior direito e selecione **Repositórios**.
3. Adicione:

   `https://github.com/joaov-meneses/home-assistant-x5-bridge`

4. Instale o add-on **X5 Bridge**.
5. Preencha as opções de configuração e inicie o add-on.

Consulte a [documentação do add-on](x5_bridge/README.md) para detalhes sobre a
configuração, sincronização e entidades criadas.

## Licença

Distribuído sob a licença MIT. Consulte [LICENSE](LICENSE).
