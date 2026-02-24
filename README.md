# Frigate Alerts

Animated GIF notifications from [Frigate NVR](https://frigate.video) events. Get instant push notifications with a preview GIF of what triggered the alert — not just a still image.

![Web UI](https://img.shields.io/badge/Web_UI-included-blue) ![Docker](https://img.shields.io/badge/Docker-ready-blue) ![License](https://img.shields.io/badge/License-MIT-green)

## Why?

Frigate generates animated preview GIFs for every detection event, but existing notification tools only send still snapshots. This project sends the actual animated GIF so you can see what happened at a glance.

**Features:**
- Animated GIF notifications (with snapshot fallback)
- **7 notification providers**: Pushover, Discord, Telegram, Ntfy, Gotify, Email (SMTP), Webhook
- **Zero dependencies** — works out of the box with just a Frigate URL
- **Optional MQTT** for faster delivery (with automatic API fallback)
- **Web UI** for configuration — no YAML editing required
- Notification history with timestamps and status
- Test notification button
- Camera and label filtering
- Multiple recipients per provider
- Tap notification to open the event in Frigate
- Single Docker container, minimal resource usage

## How It Works

Frigate Alerts has two ways to detect events, and both can run at the same time:

| Mode | Speed | Requirements | Setup |
|------|-------|-------------|-------|
| **API Polling** (default) | ~10 seconds | Just Frigate URL | Zero config |
| **MQTT** (optional) | Instant | MQTT broker details | Add broker in settings |

**By default, only API polling is used** — it queries Frigate's API every 10 seconds for new completed alerts. This means you don't need an MQTT broker, and it doesn't touch your Home Assistant or any other system.

**If you add MQTT**, both modes run simultaneously. MQTT delivers instantly, and the API poller acts as an automatic fallback if MQTT ever disconnects. Events are deduplicated so you'll never get double notifications.

```
                               ┌─── MQTT (optional, instant) ───┐
Frigate NVR ──┤                                                  ├──▶ Frigate Alerts ──▶ Notifications
                               └─── API Polling (default) ──────┘
                                                                          ↓
                                                               Fetches preview.gif
                                                               from Frigate API
```

1. Frigate Alerts detects completed alert reviews (via API polling and/or MQTT)
2. Filters by camera and label based on your settings
3. Waits briefly for Frigate to generate the preview GIF
4. Fetches the GIF from Frigate's API (`/api/events/{id}/preview.gif`)
5. Sends it as an animated attachment to all enabled providers
6. Falls back to a snapshot image if the GIF isn't available

## Supported Providers

| Provider | GIF Support | Features |
|----------|------------|----------|
| **Pushover** | Animated GIF | Multiple recipients, priority levels, custom sounds |
| **Discord** | Animated GIF | Multiple webhooks, embedded previews |
| **Telegram** | Animated GIF | Bot API, sendAnimation/sendPhoto |
| **Ntfy** | Attached image | Self-hosted or ntfy.sh, token auth |
| **Gotify** | Link only | Self-hosted, priority levels, markdown |
| **Email (SMTP)** | Attached image | Multiple recipients, TLS, any SMTP server |
| **Webhook** | JSON payload | Custom URL, headers, POST/PUT method |

## Quick Start

### 1. Deploy the container

```yaml
services:
  frigate-alerts:
    container_name: frigate-alerts
    build: https://github.com/ryzendigo/frigate-alerts.git
    restart: unless-stopped
    ports:
      - "8085:8000"
    volumes:
      - ./config:/app/config
    networks:
      - your-frigate-network  # Same network as your Frigate container
```

### 2. Open the web UI

Go to `http://your-host:8085` and configure:
- **Frigate URL** — the internal URL where this container can reach Frigate (e.g. `http://frigate:5000`)
- **At least one notification provider** — enable and configure Pushover, Discord, etc.
- **Click Save**

That's it. Notifications will start flowing on the next alert.

### 3. (Optional) Add MQTT for instant delivery

If you have an MQTT broker (Home Assistant, Mosquitto, etc.), enable MQTT in settings and add the broker details. This won't affect your existing MQTT setup — Frigate Alerts is just a passive subscriber.

## Alternative Install Methods

### Docker Run

```bash
docker run -d \
  --name frigate-alerts \
  -p 8085:8000 \
  -v $(pwd)/config:/app/config \
  --network your-frigate-network \
  ghcr.io/ryzendigo/frigate-alerts:latest
```

### Build from Source

```bash
git clone https://github.com/ryzendigo/frigate-alerts.git
cd frigate-alerts
docker build -t frigate-alerts .
docker run -d -p 8085:8000 -v $(pwd)/config:/app/config frigate-alerts
```

## Configuration

Everything can be configured through the web UI. Or edit `config/config.yml` directly — copy `config/config.example.yml` to get started:

```yaml
frigate:
  url: http://frigate:5000
  public_url: https://frigate.example.com  # for links in notifications

# Optional — leave disabled to use API polling only
mqtt:
  enabled: false
  server: 10.0.0.1
  port: 1883
  username: mqtt
  password: your_password

cameras: []   # empty = all cameras
labels:
  - person

gif_delay: 10  # seconds to wait for GIF generation

pushover:
  enabled: true
  token: your_app_token
  recipients:
    - name: User
      userkey: your_user_key
```

See `config/config.example.yml` for the full list of options including all providers.

## Provider Setup

### Pushover
1. Create an app at [pushover.net/apps](https://pushover.net/apps/build) — note the **API Token**
2. Find your **User Key** on the [Pushover dashboard](https://pushover.net/)
3. Animated GIFs require Pushover client v3.4+

### Discord
1. In your Discord server, go to **Server Settings > Integrations > Webhooks**
2. Create a webhook and copy the URL

### Telegram
1. Create a bot with [@BotFather](https://t.me/BotFather) and get the bot token
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot)

### Ntfy
1. Use [ntfy.sh](https://ntfy.sh) or self-host ntfy
2. Pick a topic name (or create one with a token for auth)

### Gotify
1. Self-host [Gotify](https://gotify.net/) and create an application
2. Use the application's token

### Email (SMTP)
1. Use any SMTP server (Gmail, Outlook, self-hosted)
2. For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833)

### Webhook
1. Point to any HTTP endpoint that accepts JSON
2. Payload includes: title, message, url, image_type, event data

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/api/config` | GET | Get current configuration |
| `/api/config` | POST | Update configuration |
| `/api/history` | GET | Notification history (`?limit=50`) |
| `/api/status` | GET | Connection status (Frigate, poller, MQTT) |
| `/api/test` | POST | Send test notification using latest event |

## Requirements

- Frigate NVR (v0.14+)
- Docker
- That's it. No MQTT broker required.

## License

MIT
