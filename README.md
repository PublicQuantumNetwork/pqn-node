# pqn-node

**FastAPI node service for the Public Quantum Network (PQN)**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Runs a PQN node: exposes the FastAPI routes used by the web UI, coordinates protocols between nodes, and orchestrates hardware through [`pqn-hardware`](https://github.com/PublicQuantumNetwork/pqn-hardware). Frontend lives in [pqn-gui](https://github.com/PublicQuantumNetwork/pqn-gui).

Hardware drivers, ZMQ messaging, and instrument protocols were extracted into [`pqn-hardware`](https://github.com/PublicQuantumNetwork/pqn-hardware) so that hardware and node work can evolve independently; `pqn-node` pulls it in as a git-pinned dependency.

<p align="center">
  <img src="docs/images/frontend_screenshot.png" alt="PQN Web Interface" width="800"/>
  <br>
  <em>PQN web interface for public interaction with a quantum network</em>
</p>

> [!WARNING]
> **Early Development**: This package is in early stages of development. APIs, installation procedures, and distribution methods are subject to change. Use in production environments is not recommended at this time.

## Node Architecture

<p align="center">
  <img src="docs/images/network_architecture.png" alt="PQN Node Architecture" width="800"/>
  <br>
  <em>PQN web interface for monitoring and controlling quantum network nodes</em>
</p>

A Node's components share an internal intranet with no external access except for quantum links to other hardware or the _Node API_.

* **Node API** (this repo): FastAPI service that handles web-UI and node-to-node communication. The only component in a Node that can talk to other components and the outside world. Entry point: `src/pqn_node/main.py`. See the [FastAPI docs](https://fastapi.tiangolo.com/deployment/) for deployment options.
* **Lightweight Web UI**: For the general public to interact with quantum networks. Lives at [pqn-gui](https://github.com/PublicQuantumNetwork/pqn-gui).
* **Router** (in `pqn-hardware`): Routes ZMQ messages between _Hardware Providers_, developers, and _Node APIs_.
* **Hardware Provider** (in `pqn-hardware`): Hosts hardware resources and exposes them through ProxyInstruments.

## Quick Start

> [!NOTE]
> **Hardware Requirements**: To do anything interesting with this software currently requires real quantum hardware components (TimeTagger, rotators, etc.). We are actively working on fully simulated hardware components to enable single-machine demos without physical devices, but this capability is not yet available.

### Prerequisites

- Python 3.12 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- Quantum hardware components (TimeTagger, compatible instruments)

### Installation

```bash
git clone https://github.com/PublicQuantumNetwork/pqn-node.git
cd pqn-node
uv sync
```

`uv sync` fetches `pqn-hardware` at the pinned commit from its GitHub repo.

### Start a Node

To fully start a PQN Node, four processes are typically needed:

* **PQN API** (this repo)
* **Router** (from `pqn-hardware`)
* **Hardware provider** (from `pqn-hardware`, optional)
* **Web GUI** (optional)

### Set up the PQN API

#### Config file

Before starting a Node API, set up a configuration file:

1. **Copy the example configuration:**
   ```bash
   cp configs/config_example.toml config.toml
   ```

> [!IMPORTANT]
> The configuration file **must** be named `config.toml` and placed at the root of the repository. If you use a different name or location, the API will not be able to find it.

2. **Edit the configuration:**
   Open `config.toml` in your editor and replace the placeholder values with your actual settings (router addresses, instrument names, etc.).

### Configure Router and Hardware Provider

Router and provider live in the `pqn-hardware` package. See [pqn-hardware's README](https://github.com/PublicQuantumNetwork/pqn-hardware#quick-start) for their config format. On the first computer on the PQN, both a router and a provider are needed; subsequent computers only need a provider.

Start the router:

```bash
uv run pqn-hw start-router --config configs/router_provider.toml
```

Start the hardware provider:

```bash
uv run pqn-hw start-provider --config configs/router_provider.toml
```

### Start the PQN API server

```bash
uv run fastapi run src/pqn_node/main.py
```

Browse protocols at http://127.0.0.1:8000/docs.

### Node host provisioning

Two routes under `/system` operate on the host itself and need one-time setup on each Node. Both are used by remote operations tooling; a Node without them still runs every protocol, it just answers those two routes with an error.

**`GET /system/screenshot`** shells out to [`maim`](https://github.com/naelstrof/maim), which writes a PNG of the whole X root window to stdout (so a multi-monitor Node returns all its screens in one image):

```bash
sudo apt install maim
```

The API process must be started from inside the desktop session — KDE autostart does this — so that it inherits `DISPLAY`, `XAUTHORITY` and `XDG_RUNTIME_DIR`. Started from a bare SSH shell, capture fails with a 503 rather than returning a black frame.

**`POST /system/reboot`** runs `sudo systemctl reboot`, so the user running the API needs to do that without a password prompt:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot" | sudo tee /etc/sudoers.d/pqn-reboot
sudo chmod 440 /etc/sudoers.d/pqn-reboot
```

The endpoint returns before the machine goes down, so the caller gets a response and can poll until the Node answers again. Recovery is unattended: on boot the machine autologs in and KDE autostart brings the API, GUI and kiosk back up.

> [!WARNING]
> Neither route is authenticated, like every other Node API route — Nodes are expected to listen only on their VPN addresses, and membership of that network is the trust boundary. Any member of it can reboot any Node.

### Install the Web GUI

See [pqn-gui](https://github.com/PublicQuantumNetwork/pqn-gui) for install and start instructions.

## Whobot

Whobot is how you operate a Network from Slack. It is the second deployable in this repo
(`pqn_whobot`), and **one** instance serves **every** Node, talking to each over the Node API.
It does not run on a Node — put it anywhere that can reach them.

Type `/whobot` in Slack and pick from a menu:

- **Daily Digest** — every Node's hardware health *and* a real CHSH and Quantum Fortune run, in
  one scheduled message. Also runnable on demand, and its schedule is changeable from the menu.
- **List Nodes / Node Info / Check one Node** — what is out there, and is it well.
- **Screenshot / Reboot** — see a Node's screen, or restart it (with a confirm step).
- **Change Game availability** — turn Games on and off without touching `config.toml`.
- **Run CHSH / Run Quantum Fortune** — a single measurement, by hand.

### Set up Whobot

**1. Create the Slack app** at <https://api.slack.com/apps> → *Create New App* → *From scratch*,
and name it Whobot. Then, in its settings:

| Page | Do this |
|---|---|
| **Socket Mode** | Toggle on, and generate an app-level token with `connections:write`. This is `slack_app_token` (`xapp-…`). |
| **OAuth & Permissions** | Bot token scopes `chat:write`, `commands`, `files:write`, `channels:read` — plus `groups:read` if the digest goes to a private channel. Install to the workspace and copy the bot token (`xoxb-…`) as `slack_bot_token`. |
| **Slash Commands** | Create `/whobot`. Leave the Request URL blank. |
| **Interactivity & Shortcuts** | Toggle on. Request URL blank here too. |

Socket Mode is why both URLs stay blank: Whobot dials **out** to Slack, so it needs no public
address, no certificate, and no inbound firewall rule. Watch the scope list — `channels:read` sits
next to `channels:history`, and the wrong one makes Whobot refuse to start.

Finally, invite the bot to the channel the digest should go to, and copy that channel's ID from
the bottom of its *About* tab.

**2. Configure and run.** `configs/whobot_example.toml` is a commented reference for every key.

```bash
cp configs/whobot_example.toml whobot.toml   # fill in: both tokens, digest_channel, each Node's address
uv sync --extra whobot                       # the Slack transport; a Node itself does not need it
uv run whobot nodes                          # check: every Node listed, named, and reachable
uv run whobot serve                          # holds the Slack connection and fires the digest
```

Whobot reads `whobot.toml` from the directory it is started in, and never a Node's `config.toml` —
its config is the Network's, not a machine's. `whobot serve` checks the tokens and the digest
channel before it starts, so a bad token or a channel the bot is not in fails immediately rather
than at 07:00.

> [!NOTE]
> Whobot has no access control of its own: anyone who can see the bot can run any Action,
> including Reboot. The channel is the audit log.

## Acknowledgements

The Public Quantum Network is supported in part by NSF Quantum Leap Challenge Institute HQAN under Award No. 2016136, Illinois Computes, and by the DOE Grant No. 712869, "Advanced Quantum Networks for Science Discovery."

## Have questions?

Contact the PQN team at publicquantumnetwork@gmail.com.
