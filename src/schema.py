import uuid

import dpath
from config import config
from loguru import logger

# Импортируем Rust функции
try:
    from openapi_merger import get_schema_sync, get_schemas_sync, merge_schemas_sync

    logger.info("Rust functions loaded successfully")
except ImportError:
    logger.error("Rust functions not available - this is required for operation")
    raise ImportError("Rust functions are required for operation")


class Schema:
    @staticmethod
    async def get_schema(url: str) -> dict:
        # Используем Rust функцию
        result = get_schema_sync(url)
        if isinstance(result, dict) and "data" in result:
            import json

            return json.loads(result["data"])
        return result

    @staticmethod
    async def get_schemas(
        sources: list[dict],
        *,
        enabled: bool = True,
    ) -> list[tuple[str, dict]]:
        """Get schemas with service names"""
        # Подготавливаем данные для Rust функции
        import json

        py_sources = []
        for source in sources:
            if source.get("enabled", enabled):
                source_dict = {
                    "name": source.get("name", str(uuid.uuid4())[:10]),
                    "schema": source.get("schema"),
                    "url": source.get("url", "http://localhost"),
                    "enabled": source.get("enabled", enabled),
                }
                py_sources.append(source_dict)

        if not py_sources:
            return []

        # Вызываем Rust функцию
        results = get_schemas_sync(py_sources, enabled)

        # Преобразуем результат в нужный формат
        schemas = []
        for result in results:
            if isinstance(result, dict):
                name = result.get("name", "")
                url = result.get("url", "")
                schema_data = result.get("schema_data", {})

                # Создаем source dict
                source = {"name": name, "url": url, "enabled": True}

                # Парсим schema_data
                if isinstance(schema_data, dict) and "data" in schema_data:
                    try:
                        schema = json.loads(schema_data["data"])
                        schemas.append((name, source, schema))
                    except json.JSONDecodeError:
                        logger.error(f"Failed to parse schema data for {name}")
                else:
                    logger.error(f"Invalid schema data format for {name}")

        return schemas


class SchemasMerger:
    def __init__(self, schemas: list[tuple[str, dict, dict]], *, grouping: bool = True):
        self.schemas = schemas
        self.grouping = grouping

    def _prepare_server_for_schema(
        self, schema: dict, *, url: str, source_name: str = None
    ) -> dict:
        """Adds server to all operations in the schema"""
        prepared_schema = schema.copy()
        proxy_enabled = config.get("settings", {}).get("proxy", False)

        # Get all paths
        paths = dpath.get(prepared_schema, "paths") or {}

        for path, operations in paths.items():
            for operation in operations.values():
                if isinstance(operation, dict):
                    # Add server to operation
                    if "servers" not in operation:
                        operation["servers"] = []

                    # Check if server already exists
                    server_exists = any(
                        server.get("url") == url for server in operation["servers"]
                    )

                    if not server_exists:
                        # If proxy is enabled and this is an external service, check Caddy availability
                        if proxy_enabled and source_name:
                            # Check if Caddy is available before creating proxy servers
                            if self._is_caddy_available():
                                # Create proxy URL for external services using the same logic as in __init__.py
                                safe_name = self._create_safe_name(source_name)
                                proxy_url = f"/proxy/{safe_name}"
                                operation["servers"].append(
                                    {
                                        "url": proxy_url,
                                        "description": f"Proxied to {url}",
                                    }
                                )
                                logger.debug(
                                    f"Added proxy server {proxy_url} for {source_name}"
                                )
                            else:
                                # Caddy not available, use original URL but log warning
                                operation["servers"].append({"url": url})
                                logger.warning(
                                    f"Proxy enabled but Caddy not available for {source_name}. "
                                    f"Using original URL: {url}"
                                )
                        else:
                            # Use original URL
                            operation["servers"].append({"url": url})

        return prepared_schema

    def _is_caddy_available(self) -> bool:
        """Check if Caddy is available using Rust function"""
        from openapi_merger.openapi_merger import is_caddy_available

        return is_caddy_available()

    def _create_safe_name(self, name: str) -> str:
        """Create URL-safe name using Rust function"""
        from openapi_merger.openapi_merger import safe_name

        return safe_name(name)

    def _prepare_grouping(self, schema: dict, *, name: str) -> dict:
        """Adds service name to schema tags for grouping"""
        global_tags = dpath.get(schema, "tags", default=[])
        for tag in global_tags:
            tag["name"] = f"{name} | {tag['name']}"
        dpath.set(schema, "tags", global_tags)

        # Process local tags in paths
        paths = dpath.get(schema, "paths", default={})

        for operations in paths.values():
            for operation in operations.values():
                for tag in operation.get("tags", []):
                    operation["tags"] = [f"{name} | {tag}"]

        dpath.set(schema, "paths", paths)
        return schema

    def merge(self) -> dict:
        """Main method for merging schemas using Rust function"""
        if not self.schemas:
            return {}

        # Используем Rust функцию для слияния
        import json

        # Подготавливаем данные для Rust функции
        rust_schemas = []
        for name, source, schema in self.schemas:
            if schema is None:
                logger.warning(f"Skipping {name} - schema failed to load")
                continue

            # Добавляем серверы к схеме
            schema_with_servers = self._prepare_server_for_schema(
                schema, url=source.get("url"), source_name=name
            )

            # Если включена группировка, добавляем имя сервиса к тегам
            if self.grouping:
                schema_with_servers = self._prepare_grouping(
                    schema_with_servers, name=name
                )

            # Создаем dict для Rust функции
            schema_dict = {
                "name": name,
                "url": source.get("url", "http://localhost"),
                "schema_data": json.dumps(schema_with_servers),
            }
            rust_schemas.append(schema_dict)

        if not rust_schemas:
            return {}

        # Вызываем Rust функцию слияния
        result = merge_schemas_sync(rust_schemas, self.grouping)

        if isinstance(result, dict) and "merged_schema" in result:
            try:
                merged_schema = json.loads(result["merged_schema"])

                # Обновляем метаданные
                dpath.set(
                    merged_schema,
                    "info/title",
                    dpath.get(config, "settings/title", default="Merged API"),
                )
                dpath.set(
                    merged_schema,
                    "info/description",
                    dpath.get(config, "settings/description", default=""),
                )
                dpath.set(
                    merged_schema,
                    "info/version",
                    dpath.get(config, "settings/version", default="1.0.0"),
                )
                merged_schema["openapi"] = "3.1.0"

                return merged_schema
            except json.JSONDecodeError:
                logger.error("Failed to parse merged schema from Rust function")
                raise RuntimeError("Failed to parse merged schema from Rust function")

        raise RuntimeError("Rust merge_schemas failed to return valid result")
