import sys

from .config import get_settings
from .server import GatewayCredentialMiddleware, create_mcp_server


def _build_http_app(mcp, settings):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    async def health(_: Request) -> JSONResponse:
        # Purely local probe: it must not depend on Exchange Online being up,
        # or an upstream wobble would get this container restarted.
        return JSONResponse({"status": "ok"})

    mcp_app = mcp.streamable_http_app()  # owns the streamable-http session-manager lifespan
    mounted = GatewayCredentialMiddleware(mcp_app, settings)

    # Mount() does NOT run a sub-app's lifespan, so the session manager's task
    # group would never start ("Task group is not initialized") while /health
    # kept answering 200. Propagate the MCP app's lifespan to the outer app.
    return Starlette(
        routes=[Route("/health", health), Mount("/", app=mounted)],
        lifespan=lambda app: mcp_app.router.lifespan_context(app),
    )


def main() -> None:
    settings = get_settings()
    mcp = create_mcp_server(settings)

    import uvicorn

    app = _build_http_app(mcp, settings)
    print(
        f"Exchange Online MCP server listening on "
        f"http://{settings.mcp_http_host}:{settings.mcp_http_port}/mcp",
        file=sys.stderr,
    )
    uvicorn.run(app, host=settings.mcp_http_host, port=settings.mcp_http_port)


if __name__ == "__main__":
    main()
