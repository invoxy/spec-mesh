import sys
from contextlib import asynccontextmanager

import uvicorn
from __init__ import set_docs, set_logger, set_mount, set_schema, set_proxy
from fastapi import FastAPI
from config import config
from schema import validate_sources_before_startup
from loguru import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_logger()

    # Валидируем источники перед запуском
    logger.info("Starting source validation...")
    sources = config.get("sources", [])
    valid_sources = await validate_sources_before_startup(sources)

    # Обновляем конфигурацию только валидными источниками
    config["sources"] = valid_sources

    set_mount(app)
    set_schema(app)
    set_proxy(app)
    set_docs(app, config["settings"])
    yield


app = FastAPI(openapi_url=None, lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True, log_level="debug")  # noqa: S104
