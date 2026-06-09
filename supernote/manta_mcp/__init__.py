"""Manta MCP — a Model Context Protocol server over the self-hosted Supernote cloud.

This is a *sibling* service to the cloud server (it talks to the cloud over the
existing client library / HTTP API), not the AI/insights MCP that ships gated-off
inside ``supernote.server``. It exposes file operations — list, read, search,
push, delete — so an agent on any tailnet machine can work with documents on the
cloud. Pushing a document auto-syncs it to the Manta device via the cloud's
existing change-event path.
"""
