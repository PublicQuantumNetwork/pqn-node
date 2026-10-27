# PQN Node

A Node in the Public Quantum Network: it runs the Games the public plays at an exhibit, coordinates two-player Games with a peer Node, and drives the lab hardware through pqn-hardware.

## Language

### Games and players

**Game**:
One of the activities a Node offers the public: Verify Quantum Link (CHSH), Quantum Fortune, or Share a Secret Message (SSM).

**Answer**:
A player's choice for one question in SSM, either *left* or *right* (`a` / `b` in the Node API). With entanglement it picks the player's measurement basis (H/V or D/A), and a random bit picks the state within it; in correlation-mode SSM it picks a fixed polarization (V or D) and there is no random bit.
_Avoid_: selection

**Operation mode**:
How a Node plays two-player Games: *entanglement* (relies on entangled pairs, today's behaviour) or *correlation* (relies only on polarization-correlated pairs, for when entanglement is low).
_Avoid_: low-entanglement mode, game mode

### Photons and measurement

**Lab photon**:
The photon of a pair that stays in the lab.

**Library photon**:
The photon of a pair that travels to the library and back over fiber.

**Coincidence level**:
How many coincidences a setting gives relative to the **Reference rates**: *high*, *medium* or *low*.

**Reference rate**:
The expected coincidences per second for VV, VD or DD, set in the leading Node's config.
_Avoid_: threshold, calibration counts

**Calibrated angles**:
The waveplate angles a Node moves to for H, V, D and A, set in its config.
_Avoid_: offsets, QKD angles

## Relationships

- In correlation-mode SSM, each player's **Answer** sets only that player's own waveplate: *left* → V, *right* → D.
- The **Coincidence level** of a question shows whether the two **Answers** agree: *high* (both left) or *low* (both right) means agree, *medium* means disagree.

## Example dialogue

> **Dev:** "Does the Node look at both **Answers** to pick the waveplate angles?"
> **Domain expert:** "No. Each player's **Answer** only moves their own waveplate. The **Coincidence level** is what tells us whether they agreed."

## Flagged ambiguities

- The whiteboard's PPS/EPS columns could be read as two pieces of hardware. They are the same source, behaving as a plain pair source (≈ product VV) or as an entangled one.
