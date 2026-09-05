"""Provider-managed agent runtime support."""

from agent_runtime.service import AgentService, get_agent_service, shutdown_agent_service

__all__ = ["AgentService", "get_agent_service", "shutdown_agent_service"]
