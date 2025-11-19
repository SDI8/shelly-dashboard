# Shelly Dashboard
A lightweight monitoring setup for Shelly devices over MQTT.

![System Architecture](./doc/graph.drawio.png)

## What it IS
- Optimized for low resource consumption, ideal for running on Raspberry Pis or similar low-power devices.
- Self-hosted, without relying on cloud services.
- Highly customizable and extensible. Telegraf supports a wide range of outputs, and Grafana offers numerous plugins and visualization options.
  
## What it is NOT
- A full-fledged home automation system. It focuses on monitoring and visualization rather than control or automation. The communication is one-way: Shelly devices publish their status via MQTT, and this setup collects and visualizes that data.
- A commercial product. It is an open-source project intended for personal use and experimentation.
- Secured by default.There is no encryption or authentication configured for most services. It is recommended to implement appropriate security measures based on your deployment environment.
- Complete out-of-the-box solution. Some assembly required. Grafana contains no pre-built dashboards. Likewise, I do not have every Shelly device, so you may need to tweak configurations to suit your specific devices and use cases. Contributions are welcome!

## Setup
1. Ensure you have Docker and Docker Compose installed on your system.
2. Run `docker-compose up -d` in the project directory to start all services.
3. Configure your Shelly device's MQTT settings to the broker address (default: `<host-ip>:1883`). Keep the default MQTT prefix, this allows Telegraf to parse the shelly model and device ID. Disable "MQTT Control" and "RPC over MQTT".
4. Access Grafana at `http://<host-ip>:3000` (default credentials: admin/admin) and have fun creating dashboards!


## Development
The main work is done by Telegraf, which has parsers for the most common MQTT topics.

The project focuses on minimal resource usage by default, you might want to adjust resource limits in `docker-compose.yml` based on your hardware capabilities.

### Future improvements
- Add support for more Shelly device models.
- Handle data gaps for devices which have local storage.
- Add pre-configured Grafana dashboards for common Shelly devices.
- Improve security options.

## History
- *19. November 2025:* Project start.