"""Whobot — the operations bot for a PQN Network. See ``WHOBOT.md``.

One Whobot instance serves many Nodes: it posts the scheduled Daily Digest and lets an
operator probe and control any Node from a chat platform.

Two rules this package is built to, both enforced by tests:

- ``pqn_whobot`` may import from ``pqn_node``; nothing in ``pqn_node`` may import from
  ``pqn_whobot``.
- Nothing here assumes which machine it runs on: no ``localhost`` defaults, every Node
  addressed from the registry in ``whobot.toml``, and no reliance on the host's timezone.
"""
