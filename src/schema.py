import asyncio
import uuid

import dpath
import httpx
from config import config
from loguru import logger
from yaml import safe_load
from dpath.util import get, set


class Schema:
    @staticmethod
    async def get_schema(url: str) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            content_type = response.headers.get("content-type", "").lower()

            # OpenAPI спецификации (любого формата)
            if "vnd.oai.openapi" in content_type:
                try:
                    return response.json()  # Пробуем JSON
                except:
                    return safe_load(response.text)

            # Обычные форматы
            if "json" in content_type:
                return response.json()
            if "yaml" in content_type:
                return safe_load(response.text)
        return None

    @staticmethod
    async def get_schemas(
        sources: list[dict],
        *,
        enabled: bool = True,
    ) -> list[tuple[str, dict]]:
        """Получаем схемы с именами сервисов"""
        tasks = []
        for source in sources:
            schema_url = source.get("schema")  # Используем 'schema' для загрузки схемы
            name = source.get("name", str(uuid.uuid4())[:10])
            if source.get("enabled", enabled):
                tasks.append((name, source, __class__.get_schema(schema_url)))

        results = await asyncio.gather(
            *[task[2] for task in tasks], return_exceptions=True
        )

        # Фильтруем ошибки и возвращаем кортежи (имя, source, схема)
        schemas = []
        for i, (name, source, task) in enumerate(tasks):
            result = results[i]
            if isinstance(result, Exception):
                logger.error(f"Ошибка загрузки {name} ({task}): {result}")
            elif result is None:
                logger.error(f"Схема {name} не загрузилась (результат None)")
            else:
                schemas.append((name, source, result))

        return schemas


class SchemasMerger:
    def __init__(self, schemas: list[tuple[str, dict, dict]], *, grouping: bool = True):
        self.schemas = schemas
        self.grouping = grouping

        self.merged = {}
        self.merged_paths = {}
        self.merged_schemas = {}

    def _merge_schemas(self) -> dict:
        """Объединяет схемы компонентов из всех сервисов"""
        merged_schemas = {}

        for service_name, source, schema in self.schemas:
            schemas_dict = get(schema, "components/schemas") or {}

            for schema_name, schema_def in schemas_dict.items():
                if schema_name in merged_schemas:
                    # Разрешаем конфликты схем
                    new_name = f"{schema_name}_{service_name}"
                    logger.warning(
                        f"Схема {schema_name} конфликтует, переименовываю в {new_name}"
                    )
                    merged_schemas[new_name] = schema_def
                else:
                    merged_schemas[schema_name] = schema_def

        return merged_schemas

    def _merge_paths(self) -> dict:
        """Объединяет пути из всех сервисов"""
        merged_paths = {}

        for service_name, source, schema in self.schemas:
            paths = get(schema, "paths") or {}

            for path, methods in paths.items():
                if path in merged_paths:
                    # Разрешаем конфликты путей
                    new_path = f"{path}_{service_name}"
                    logger.warning(
                        f"Путь {path} конфликтует, переименовываю в {new_path}"
                    )
                    merged_paths[new_path] = methods
                else:
                    merged_paths[path] = methods

        return merged_paths

    def _merge_components(self) -> dict:
        """Объединяет все компоненты из всех сервисов"""
        merged_components = {}

        for service_name, source, schema in self.schemas:
            components = get(schema, "components") or {}

            for component_type, component_data in components.items():
                if component_type == "schemas":
                    # Схемы обрабатываются отдельно в _merge_schemas
                    continue

                if component_type not in merged_components:
                    merged_components[component_type] = {}

                if isinstance(component_data, dict):
                    for name, definition in component_data.items():
                        if name in merged_components[component_type]:
                            # Разрешаем конфликты
                            new_name = f"{name}_{service_name}"
                            logger.warning(
                                f"Компонент {component_type}/{name} конфликтует, переименовываю в {new_name}"
                            )
                            merged_components[component_type][new_name] = definition
                        else:
                            merged_components[component_type][name] = definition

        return merged_components

    def _prepare_server_for_schema(self, schema: dict, *, url: str) -> dict:
        """Добавляет сервер во все операции схемы"""
        prepared_schema = schema.copy()

        # Получаем все пути
        paths = get(prepared_schema, "paths") or {}

        for path, operations in paths.items():
            for operation in operations.values():
                if isinstance(operation, dict):
                    # Добавляем сервер к операции
                    if "servers" not in operation:
                        operation["servers"] = []

                    # Проверяем, нет ли уже такого сервера
                    server_exists = any(
                        server.get("url") == url for server in operation["servers"]
                    )

                    if not server_exists:
                        operation["servers"].append({"url": url})

        return prepared_schema

    def _prepare_grouping(self, schema: dict, *, name: str) -> dict:
        """Добавляет имя сервиса к тегам схемы для группировки"""
        global_tags = dpath.get(schema, "tags", default=[])
        for tag in global_tags:
            tag["name"] = f"{name} | {tag['name']}"
        dpath.set(schema, "tags", global_tags)

        # Обрабатываем локальные теги в путях
        paths = dpath.get(schema, "paths", default={})
        from pprint import pprint

        for operations in paths.values():
            for operation in operations.values():
                for tag in operation.get("tags", []):
                    operation["tags"] = [f"{name} | {tag}"]

        dpath.set(schema, "paths", paths)
        return schema

    def merge(self) -> dict:
        """Основной метод объединения схем"""
        if not self.schemas:
            return {}

        # Подготавливаем схемы с серверами
        for service_name, source, schema in self.schemas:
            if schema is None:
                logger.warning(f"Пропускаем {service_name} - схема не загрузилась")
                continue

            logger.info(f"=== {service_name} ===")
            logger.info(f"Добавляем сервер {source.get('url')} к схеме")
            schema = self._prepare_server_for_schema(schema, url=source.get("url"))

            # Если включена группировка, добавляем имя сервиса к тегам
            if self.grouping:
                schema = self._prepare_grouping(schema, name=service_name)

        # Объединяем все компоненты
        self.merged_paths = self._merge_paths()
        self.merged_schemas = self._merge_schemas()
        merged_components = self._merge_components()

        # Собираем финальную схему
        if self.schemas:
            first_schema = self.schemas[0][
                2
            ]  # Берем схему из кортежа (name, source, schema)
            self.merged = first_schema.copy()

            # Устанавливаем объединенные компоненты
            set(self.merged, "paths", self.merged_paths)

            # Создаем компоненты если их нет
            if "components" not in self.merged:
                set(self.merged, "components", {})

            # Добавляем схемы
            set(self.merged, "components/schemas", self.merged_schemas)

            # Добавляем остальные компоненты
            for component_type, component_data in merged_components.items():
                set(self.merged, f"components/{component_type}", component_data)

        # Обновляем метаинформацию с помощью dpath
        set(
            self.merged,
            "info/title",
            get(config, "settings/title") or "Merged API",
        )
        set(
            self.merged,
            "info/description",
            get(config, "settings/description") or "",
        )
        set(
            self.merged,
            "info/version",
            get(config, "settings/version") or "1.0.0",
        )

        return self.merged
