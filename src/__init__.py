import sys
from pathlib import Path

from config import STATIC_DIR, config
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template
from loguru import logger
from schema import Schema, SchemasMerger, validate_sources_before_startup


def set_docs(app: FastAPI, config: dict):
    @app.get("/", include_in_schema=False)
    async def docs():
        with Path.open(STATIC_DIR / "swagger-ui.html") as f:
            html_content = f.read()
        html_content = Template(html_content).render(config=config)
        return HTMLResponse(
            content=html_content,
            media_type="text/html",
        )


def set_schema(app: FastAPI):
    @app.get("/openapi.json", include_in_schema=False)
    async def schema():
        sources = config.get("sources", [])
        # Используем только валидные источники
        valid_sources = await validate_sources_before_startup(sources)
        schemas = await Schema.get_schemas(valid_sources)
        merger = SchemasMerger(schemas)
        return merger.merge()


def set_mount(app: FastAPI):
    app.mount("/swagger/", StaticFiles(directory=STATIC_DIR))


def set_proxy(app: FastAPI):
    """Set up proxy routes for external API specifications"""
    sources = config.get("sources", [])
    proxy_enabled = config.get("settings", {}).get("proxy", False)

    if not proxy_enabled:
        return

    # Check if Caddy is available (running in container or system)
    caddy_available = _check_caddy_availability()

    if not caddy_available:
        logger.warning(
            "Proxy is enabled in config but Caddy is not available. "
            "Proxy routes will not be created. "
            "Make sure Caddy is running or set proxy: false in config.yml"
        )
        return

    logger.info("Caddy detected, setting up proxy routes...")

    # Валидируем источники перед генерацией Caddyfile
    async def validate_and_generate():
        valid_sources = await validate_sources_before_startup(sources)
        _generate_caddyfile(valid_sources)
    
    # Запускаем валидацию синхронно для совместимости
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Если уже в event loop, создаем задачу
            asyncio.create_task(validate_and_generate())
        else:
            # Если нет event loop, запускаем новый
            asyncio.run(validate_and_generate())
    except RuntimeError:
        # Fallback для случаев без event loop
        logger.warning("Could not validate sources asynchronously, proceeding with original sources")
        _generate_caddyfile(sources)

    for source in sources:
        if not source.get("enabled", True):
            continue

        source_url = source.get("url", "").rstrip("/")
        schema_url = source.get("schema", "").rstrip("/")

        if not source_url or not schema_url:
            continue

        # Create proxy route for the source URL
        @app.api_route(
            "/proxy/{source_name}/{path:path}",
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )
        async def proxy_request(source_name: str, path: str, request: Request):
            # Find the source by name
            target_source = None
            for src in sources:
                # Create URL-safe name for comparison
                safe_name = _create_safe_name(src.get("name", ""))
                if safe_name == source_name and src.get("enabled", True):
                    target_source = src
                    break

            if not target_source:
                return {"error": f"Source {source_name} not found or disabled"}

            target_url = target_source.get("url", "").rstrip("/")
            if not target_url:
                return {"error": f"Invalid URL for source {source_name}"}

            # Build the full target URL
            full_url = f"{target_url}/{path}"

            # Get query parameters
            query_params = str(request.query_params) if request.query_params else ""
            if query_params:
                full_url += f"?{query_params}"

            # Redirect to the target URL
            return RedirectResponse(url=full_url, status_code=307)

        # Create a more specific proxy route for the exact source
        safe_source_name = _create_safe_name(source.get("name", ""))
        if safe_source_name:

            @app.api_route(
                f"/proxy/{safe_source_name}/{{path:path}}",
                methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                include_in_schema=False,
            )
            async def proxy_specific_source(path: str, request: Request):
                target_url = source_url
                full_url = f"{target_url}/{path}"

                # Get query parameters
                query_params = str(request.query_params) if request.query_params else ""
                if query_params:
                    full_url += f"?{query_params}"

                # Redirect to the target URL
                return RedirectResponse(url=full_url, status_code=307)


def _generate_caddyfile(sources: list):
    """Automatically generate Caddyfile for external API sources"""
    with Path.open(Path("/app/src") / "caddyfile.template") as f:
        caddy_template = f.read()
        # Prepare data for template
        external_sources = []
        for source in sources:
            if source.get("enabled", True):
                source_url = source.get("url", "").rstrip("/")
                # Check if it's external (not localhost:8000)
                if not any(
                    local in source_url
                    for local in ["localhost:8000", "127.0.0.1:8000", "0.0.0.0:8000"]
                ):
                    source_copy = source.copy()
                    source_copy["safe_name"] = _create_safe_name(source.get("name", ""))
                    external_sources.append(source_copy)

        # Generate Caddyfile
        template = Template(caddy_template)
        caddy_config = template.render(sources=external_sources)

        # Write to /etc/caddy/Caddyfile (for Caddy) and local Caddyfile (for reference)
        caddy_paths = [Path("/etc/caddy/Caddyfile"), Path("Caddyfile")]

        for caddy_path in caddy_paths:
            try:
                caddy_path.parent.mkdir(parents=True, exist_ok=True)
                with open(caddy_path, "w", encoding="utf-8") as f:
                    f.write(caddy_config)
                logger.info(f"Caddyfile generated at: {caddy_path}")
            except Exception as e:
                logger.warning(f"Could not write Caddyfile to {caddy_path}: {e}")

        logger.info(
            f"Generated Caddyfile with {len(external_sources)} external sources:"
        )
        for source in external_sources:
            logger.info(f"  - {source['name']} -> /{source['safe_name']}/")


def _create_safe_name(name: str) -> str:
    """Create URL-safe name by removing/replacing special characters"""
    from openapi_merger.openapi_merger import safe_name

    return safe_name(name)


def _check_caddy_availability() -> bool:
    """Check if Caddy is available (running in container or system)"""
    from openapi_merger.openapi_merger import is_caddy_available

    return is_caddy_available()


def set_logger():
    logger.remove()

    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>",  # noqa: E501
        level="INFO",
        colorize=True,
    )


__all__ = ["set_docs", "set_logger", "set_mount", "set_schema", "set_proxy"]
