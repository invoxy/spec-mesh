import json

import dpath
from config import config
from loguru import logger

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
        # Используем оптимизированную Rust функцию
        results = process_sources_with_uuid_rust(sources, enabled)

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
            try:
                merged_schema = json.loads(result["merged_schema"])

                # Используем Rust функцию для обновления метаданных
                metadata_json = json.dumps(merged_schema)
                config_json = json.dumps(config)
                updated_json = update_schema_metadata_rust(
                    metadata_json,
                    get_config_value_rust(config_json, "settings/title", "Merged API"),
                    get_config_value_rust(config_json, "settings/description", ""),
                    get_config_value_rust(config_json, "settings/version", "1.0.0"),
                )

                return json.loads(updated_json)

            except json.JSONDecodeError:
                logger.error("Failed to parse merged schema from Rust function")
                raise RuntimeError("Failed to parse merged schema from Rust function")

        raise RuntimeError("Rust merge_schemas failed to return valid result")
