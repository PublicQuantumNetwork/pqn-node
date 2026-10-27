# PQN Node

A Node runs one station of the Public Quantum Network. Whobot watches a fleet of Nodes from Slack and reports on them.

## Language

### Fleet and monitoring

**Node**:
One station of the network, made up of its API, hardware, the GUI and the Kiosk.
_Avoid_: station, machine (when the whole Node is meant)

**Whobot**:
The Slack bot that runs Actions against one or more Nodes.

**Daily Digest**:
Whobot's scheduled report covering every registered Node's Check-up.
_Avoid_: daily report

**Check-up**:
One Node's health check plus its unattended Games, as it appears in the Daily Digest or in "Check one Node".

**Screenshot**:
A picture of everything the Node's display is showing.

### Node components

**GUI**:
The pqn-gui web server (Next.js) that serves the visitor-facing interface on the Node.
_Avoid_: frontend, web UI (when the server process is meant)

**Kiosk**:
The full-screen browser on the Node's display that shows the GUI to visitors.

**Availability gate**:
The rule that switches a Game off while the hardware it needs is unreachable.

## Relationships

- The **Daily Digest** contains one **Check-up** per registered **Node**
- A **Kiosk** shows exactly one **GUI**, the one on its own **Node**
- The **GUI** is part of a **Node**'s health, but it is not an input to the **Availability gate**

## Example dialogue

> **Dev:** "If the **GUI** is down, does the **Daily Digest** skip that **Node**'s Games?"
> **Domain expert:** "No. The **Availability gate** only covers hardware the Games need. The **Check-up** still plays CHSH and QF and marks the **Node** as failing because of the **GUI**."

## Flagged ambiguities

- "GUI was down" could mean the **GUI** server, the **Kiosk** browser, or the display session. Resolved: the health check probes the **GUI** server. A **Screenshot** is the only thing that catches a dead **Kiosk** or a broken session.
