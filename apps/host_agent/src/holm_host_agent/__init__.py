"""Private AI Workspace Host Agent process boundary."""

from holm_host_agent._grpc_fork_safety import configure_exec_only_children

configure_exec_only_children()

from holm_host_agent.server import AGENT_SOCKET_PATH, HostAgentServer  # noqa: E402

__all__ = ["AGENT_SOCKET_PATH", "HostAgentServer"]
