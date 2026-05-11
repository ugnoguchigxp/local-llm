import atexit
import asyncio
import os
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _is_truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: List[str]
    env: Dict[str, str]


class VibeMcpClient:
    def __init__(
        self,
        root_dir: str,
        server_configs: Optional[List[McpServerConfig]] = None,
        gnosis_root_dir: Optional[str] = None,
    ):
        self.root_dir = os.path.abspath(root_dir)
        self.gnosis_root_dir = os.path.abspath(
            gnosis_root_dir
            or os.getenv("GNOSIS_MCP_SERVER_ROOT", "")
            or os.path.join(os.path.dirname(__file__), "../../..")
        )
        self.server_configs: List[McpServerConfig] = (
            server_configs if server_configs is not None else self._build_default_server_configs()
        )
        self.sessions: Dict[str, ClientSession] = {}
        self._client_contexts: Dict[str, Any] = {}
        self._tool_routes: Dict[str, Tuple[str, str]] = {}
        self._tools_cache: List[Dict[str, Any]] = []
        self.startup_errors: Dict[str, str] = {}

        atexit.register(self._atexit_shutdown)

    def _mcp_log_path(self) -> str:
        log_dir = os.path.join(self.root_dir, ".debug")
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, "mcp_server.log")

    def _build_default_server_configs(self) -> List[McpServerConfig]:
        configs: List[McpServerConfig] = []

        gnosis_enabled = _is_truthy(os.getenv("LOCAL_LLM_MCP_ENABLE_GNOSIS"), default=True)
        if gnosis_enabled:
            log_path = self._mcp_log_path()
            index_path = os.path.join(self.gnosis_root_dir, "src/index.ts")
            configs.append(
                McpServerConfig(
                    name="gnosis",
                    command="bash",
                    args=[
                        "-c",
                        f"cd {shlex.quote(self.gnosis_root_dir)} && bun run {shlex.quote(index_path)} 2>> {shlex.quote(log_path)}",
                    ],
                    env={
                        "NODE_ENV": "development",
                        "GNOSIS_NO_WORKERS": "true",
                    },
                )
            )

        semantic_enabled = _is_truthy(os.getenv("LOCAL_LLM_MCP_ENABLE_SEMANTIC"), default=False)
        if semantic_enabled:
            semantic_command = os.getenv("LOCAL_LLM_SEMANTIC_COMMAND", "bun").strip() or "bun"
            semantic_args_env = os.getenv("LOCAL_LLM_SEMANTIC_ARGS")
            if semantic_args_env and semantic_args_env.strip():
                semantic_args = shlex.split(semantic_args_env)
            else:
                semantic_args = [
                    "run",
                    os.path.join(self.gnosis_root_dir, "src/scripts/semanticCodeMcpServer.ts"),
                ]

            semantic_env: Dict[str, str] = {
                "GNOSIS_ROOT_DIR": self.root_dir,
            }
            semantic_env_raw = os.getenv("LOCAL_LLM_SEMANTIC_ENV")
            if semantic_env_raw and semantic_env_raw.strip():
                # format: KEY=VALUE,ANOTHER=VALUE
                for pair in semantic_env_raw.split(","):
                    if "=" not in pair:
                        continue
                    key, value = pair.split("=", 1)
                    key = key.strip()
                    if key:
                        semantic_env[key] = value.strip()

            log_path = self._mcp_log_path()
            
            # Use bash -c to change directory and redirect stderr
            configs.append(
                McpServerConfig(
                    name="semantic",
                    command="bash",
                    args=[
                        "-c",
                        f"cd {shlex.quote(self.gnosis_root_dir)} && {shlex.quote(semantic_command)} {' '.join(shlex.quote(a) for a in semantic_args)} 2>> {shlex.quote(log_path)}",
                    ],
                    env=semantic_env,
                )
            )

        return configs

    def _atexit_shutdown(self) -> None:
        if not self.sessions:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.create_task(self.stop())
            return

        try:
            asyncio.run(self.stop())
        except Exception:
            pass

    async def start(self):
        """Spawn configured MCP servers and connect to them."""
        if self.sessions or self._client_contexts:
            await self.stop()

        errors: List[str] = []
        self.startup_errors = {}

        for config in self.server_configs:
            client_context = None
            session = None
            try:
                server_params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env={**os.environ, **config.env},
                )
                client_context = stdio_client(server_params)
                read, write = await client_context.__aenter__()
                session = ClientSession(read, write)
                await session.__aenter__()
                await session.initialize()

                self._client_contexts[config.name] = client_context
                self.sessions[config.name] = session
            except Exception as exc:
                self.startup_errors[config.name] = str(exc)
                errors.append(f"{config.name}: {exc}")

                if session is not None:
                    try:
                        await session.__aexit__(None, None, None)
                    except Exception:
                        pass
                if client_context is not None:
                    try:
                        await client_context.__aexit__(None, None, None)
                    except Exception:
                        pass

        if not self.sessions:
            detail = "; ".join(errors) if errors else "no MCP server configs available"
            raise RuntimeError(f"Failed to start any MCP servers ({detail})")

        await self._refresh_tools_cache()

    async def _refresh_tools_cache(self) -> List[Dict[str, Any]]:
        raw_tools: List[Dict[str, Any]] = []
        for server_name, session in self.sessions.items():
            try:
                result = await session.list_tools()
            except Exception:
                continue

            for tool in result.tools:
                raw_tools.append(
                    {
                        "server": server_name,
                        "originalName": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.inputSchema,
                    }
                )

        name_counts: Dict[str, int] = {}
        for tool in raw_tools:
            original = tool["originalName"]
            name_counts[original] = name_counts.get(original, 0) + 1

        routes: Dict[str, Tuple[str, str]] = {}
        merged_tools: List[Dict[str, Any]] = []
        for tool in raw_tools:
            original = tool["originalName"]
            server_name = tool["server"]
            exposed_name = original if name_counts[original] == 1 else f"{server_name}__{original}"

            routes[exposed_name] = (server_name, original)
            merged_tools.append(
                {
                    "name": exposed_name,
                    "description": tool["description"],
                    "inputSchema": tool["inputSchema"],
                    "server": server_name,
                    "originalName": original,
                }
            )

        self._tool_routes = routes
        self._tools_cache = merged_tools
        return merged_tools

    async def list_tools(self) -> List[Dict[str, Any]]:
        if not self.sessions:
            return []
        if not self._tools_cache:
            return await self._refresh_tools_cache()
        return self._tools_cache

    def get_startup_errors(self) -> Dict[str, str]:
        return dict(self.startup_errors)

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if not self.sessions:
            raise RuntimeError("MCP sessions not started")

        if not self._tool_routes:
            await self._refresh_tools_cache()

        route = self._tool_routes.get(name)
        if not route and "__" in name:
            candidate_server, candidate_original = name.split("__", 1)
            if candidate_server in self.sessions:
                route = (candidate_server, candidate_original)

        if not route:
            candidates = [
                (server_name, original_name)
                for server_name, original_name in self._tool_routes.values()
                if original_name == name
            ]
            if len(candidates) == 1:
                route = candidates[0]

        if not route:
            raise RuntimeError(f"Unknown MCP tool '{name}'")

        server_name, original_name = route
        session = self.sessions.get(server_name)
        if not session:
            raise RuntimeError(f"MCP session for server '{server_name}' is unavailable")

        result = await session.call_tool(original_name, arguments)
        return result.content

    async def stop(self):
        for name, session in list(self.sessions.items()):
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self.sessions.pop(name, None)

        for name, context in list(self._client_contexts.items()):
            try:
                await context.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._client_contexts.pop(name, None)

        self._tool_routes = {}
        self._tools_cache = []
        self.startup_errors = {}

async def test_client():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
    client = VibeMcpClient(root)
    try:
        print("[MCP] Starting MCP servers...")
        await client.start()
        print("[MCP] Fetching tools...")
        tools = await client.list_tools()
        for t in tools:
            server = t.get("server", "unknown")
            print(f" - [{server}] {t['name']}: {t['description']}")
        
        # Test call if needed
        # res = await client.call_tool("mcp_gnosis_query_graph", {"query": "TDD"})
        # print(res)
    finally:
        await client.stop()

if __name__ == "__main__":
    asyncio.run(test_client())
