import json
import asyncio
import httpx

import dpath
from config import config
from loguru import logger
import openapi_merger

# Импортируем Rust функции
try:
    from openapi_merger import (
        get_schema_sync,
        get_schemas_sync,
        merge_schemas_sync,
        prepare_server_for_schema_rust,
        prepare_grouping_rust,
        update_schema_metadata_rust,
        process_sources_rust,
        process_schemas_batch_rust,
        get_config_value_rust,
        validate_schema_rust,
        generate_uuid_short,
        process_sources_with_uuid_rust,
    )

    logger.info("Rust functions loaded successfully")
except ImportError:
    logger.error("Rust functions not available - this is required for operation")
    raise ImportError("Rust functions are required for operation")


async def check_url_availability(url: str, timeout: float = 3.0) -> bool:
    """Check if URL is available with a short timeout"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.head(url)
            return response.status_code < 400
    except Exception:
        return False


async def validate_sources_before_startup(sources: list[dict]) -> list[dict]:
    """Validate all external sources before startup and filter out unavailable ones"""
    logger.info("Validating external API sources before startup...")

    valid_sources = []
    tasks = []

    for source in sources:
        if not source.get("enabled", True):
            continue

        schema_url = source.get("schema", "")
        if not schema_url:
            continue

        # Создаем задачу для проверки каждого URL
        task = asyncio.create_task(check_url_availability(schema_url))
        tasks.append((source, task))

    # Выполняем все проверки параллельно
    for source, task in tasks:
        try:
            is_available = await task
            if is_available:
                valid_sources.append(source)
                logger.info(
                    f"✅ Source '{source.get('name', 'Unknown')}' is available: {source.get('schema')}"
                )
            else:
                logger.warning(
                    f"❌ Source '{source.get('name', 'Unknown')}' is unavailable: {source.get('schema')}"
                )
        except Exception as e:
            logger.error(
                f"❌ Error checking source '{source.get('name', 'Unknown')}': {e}"
            )

    logger.info(
        f"Found {len(valid_sources)} available sources out of {len(sources)} total"
    )
    return valid_sources


async def get_schema_with_timeout(url: str, timeout: float = 5.0) -> dict:
    """Get schema with timeout using httpx"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()

            if "vnd.oai.openapi" in content_type or "json" in content_type:
                return response.json()
            elif "yaml" in content_type or "yml" in content_type:
                import yaml

                return yaml.safe_load(response.text)
            else:
                # Try JSON as fallback
                try:
                    return response.json()
                except:
                    import yaml

                    return yaml.safe_load(response.text)

    except httpx.TimeoutException:
        logger.warning(f"Timeout ({timeout}s) while fetching schema from {url}")
        return None
    except httpx.HTTPStatusError as e:
        logger.warning(
            f"HTTP error {e.response.status_code} while fetching schema from {url}"
        )
        return None
    except Exception as e:
        logger.warning(f"Error fetching schema from {url}: {e}")
        return None


class Schema:
    @staticmethod
    async def get_schema(url: str) -> dict:
        # Используем новую функцию с таймаутом
        return await get_schema_with_timeout(url)

    @staticmethod
    async def get_schemas(
        sources: list[dict],
        *,
        enabled: bool = True,
    ) -> list[tuple[str, dict]]:
        # Получаем схемы через Python с таймаутом
        schemas = []
        for source in sources:
            if not source.get("enabled", True):
                continue

            name = source.get("name", "")
            url = source.get("url", "")
            schema_url = source.get("schema", "")

            if not schema_url:
                continue

            logger.info(f"Fetching schema from {schema_url}")
            schema_data = await get_schema_with_timeout(schema_url)

            if schema_data:
                schemas.append((name, source, schema_data))
                logger.info(f"Successfully loaded schema for {name}")
            else:
                logger.warning(f"Failed to load schema for {name}")

        return schemas


class SchemasMerger:
    def __init__(self, schemas: list[tuple[str, dict, dict]], *, grouping: bool = True):
        self.schemas = schemas
        self.grouping = grouping

    def _prepare_server_for_schema(
        self, schema: dict, *, url: str, source_name: str = None
    ) -> dict:
        """Adds server to all operations in the schema using Rust"""
        import json

        # Конвертируем схему в JSON строку для Rust функции
        schema_json = json.dumps(schema)

        # Используем оптимизированную Rust функцию
        result_json = prepare_server_for_schema_rust(schema_json, url, source_name)

        # Парсим результат обратно
        return json.loads(result_json)

    def _is_caddy_available(self) -> bool:
        """Check if Caddy is available using Rust function"""
        from openapi_merger.openapi_merger import is_caddy_available

        return is_caddy_available()

    def _create_safe_name(self, name: str) -> str:
        """Create URL-safe name using Rust function"""
        from openapi_merger.openapi_merger import safe_name

        return safe_name(name)

    def _prepare_grouping(self, schema: dict, *, name: str) -> dict:
        """Adds service name to schema tags for grouping using Rust"""
        import json

        # Конвертируем схему в JSON строку для Rust функции
        schema_json = json.dumps(schema)

        # Используем оптимизированную Rust функцию
        result_json = prepare_grouping_rust(schema_json, name)

        # Парсим результат обратно
        return json.loads(result_json)

    def merge(self) -> dict:
        """Main method for merging schemas using Rust function"""
        if not self.schemas:
            return {}

        # Подготавливаем данные для Rust функции
        rust_schemas = []
        for name, source, schema in self.schemas:
            if schema is None:
                logger.warning(f"Skipping {name} - schema failed to load")
                continue

            # Валидируем схему с помощью Rust
            schema_json = json.dumps(schema)
            if not validate_schema_rust(schema_json):
                logger.warning(f"Skipping {name} - invalid schema format")
                continue

            # Создаем dict для Rust функции
            schema_dict = {
                "name": name,
                "url": source.get("url", "http://localhost"),
                "schema_data": schema_json,
            }
            rust_schemas.append(schema_dict)

        if not rust_schemas:
            return {}

        # Используем batch обработку для подготовки схем
        processed_schemas = process_schemas_batch_rust(rust_schemas, self.grouping)

        # Создаем обновленные схемы для слияния
        final_schemas = []
        for i, processed_schema in enumerate(processed_schemas):
            name = rust_schemas[i]["name"]
            url = rust_schemas[i]["url"]
            final_schemas.append(
                {
                    "name": name,
                    "url": url,
                    "schema_data": processed_schema,
                }
            )

        # Вызываем Rust функцию слияния
        result = merge_schemas_sync(final_schemas, False)  # Группировка уже применена

        if isinstance(result, dict) and "merged_schema" in result:
            return json.loads(result["merged_schema"])
        else:
            logger.error("Failed to merge schemas")
            return {}
