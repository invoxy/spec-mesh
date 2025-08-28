import sys
from pathlib import Path

from config import STATIC_DIR, config
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template
from loguru import logger
from schema import Schema, SchemasMerger


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
        schemas = await Schema.get_schemas(sources)
        merger = SchemasMerger(schemas)
        return merger.merge()


def set_mount(app: FastAPI):
    app.mount("/swagger/", StaticFiles(directory=STATIC_DIR))


def set_logger():
    logger.remove()

    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>",  # noqa: E501
        level="INFO",
        colorize=True,
    )


__all__ = ["set_docs", "set_logger", "set_mount", "set_schema"]
