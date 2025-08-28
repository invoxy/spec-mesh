import asyncio
import uuid

import dpath
import httpx
from config import config
from loguru import logger
from yaml import safe_load


class Schema:
    @staticmethod
    async def get_schema(url: str) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            content_type = response.headers.get("content-type", "").lower()

            # OpenAPI specifications (any format)
            if "vnd.oai.openapi" in content_type:
                try:
                    return response.json()  # Try JSON
                except:
                    return safe_load(response.text)

            # Regular formats
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
        """Get schemas with service names"""
        tasks = []
        for source in sources:
            schema_url = source.get("schema")  # Use 'schema' for loading schema
            name = source.get("name", str(uuid.uuid4())[:10])
            if source.get("enabled", enabled):
                tasks.append((name, source, __class__.get_schema(schema_url)))

        results = await asyncio.gather(
            *[task[2] for task in tasks], return_exceptions=True
        )

        # Filter errors and return tuples (name, source, schema)
        schemas = []
        for i, (name, source, task) in enumerate(tasks):
            result = results[i]
            if isinstance(result, Exception):
                logger.error(f"Error loading {name} ({task}): {result}")
            elif result is None:
                logger.error(f"Schema {name} failed to load (result is None)")
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
        """Merges component schemas from all services"""
        merged_schemas = {}

        for service_name, _, schema in self.schemas:
            schemas_dict = dpath.get(schema, "components/schemas") or {}

            for schema_name, schema_def in schemas_dict.items():
                if schema_name in merged_schemas:
                    # Resolve schema conflicts
                    new_name = f"{schema_name}_{service_name}"
                    logger.warning(
                        f"Schema {schema_name} conflicts, renaming to {new_name}"
                    )
                    merged_schemas[new_name] = schema_def
                else:
                    merged_schemas[schema_name] = schema_def

        return merged_schemas

    def _merge_paths(self) -> dict:
        """Merges paths from all services"""
        merged_paths = {}

        for service_name, _, schema in self.schemas:
            paths = dpath.get(schema, "paths") or {}

            for path, methods in paths.items():
                if path in merged_paths:
                    # Resolve path conflicts
                    new_path = f"{path}_{service_name}"
                    logger.warning(f"Path {path} conflicts, renaming to {new_path}")
                    merged_paths[new_path] = methods
                else:
                    merged_paths[path] = methods

        return merged_paths

    def _merge_components(self) -> dict:
        """Merges all components from all services"""
        merged_components = {}

        for service_name, source, schema in self.schemas:
            components = dpath.get(schema, "components") or {}

            for component_type, component_data in components.items():
                if component_type == "schemas":
                    # Schemas are processed separately in _merge_schemas
                    continue

                if component_type not in merged_components:
                    merged_components[component_type] = {}

                if isinstance(component_data, dict):
                    for name, definition in component_data.items():
                        if name in merged_components[component_type]:
                            # Resolve conflicts
                            new_name = f"{name}_{service_name}"
                            logger.warning(
                                f"Component {component_type}/{name} conflicts, renaming to {new_name}"
                            )
                            merged_components[component_type][new_name] = definition
                        else:
                            merged_components[component_type][name] = definition

        return merged_components

    def _prepare_server_for_schema(self, schema: dict, *, url: str) -> dict:
        """Adds server to all operations in the schema"""
        prepared_schema = schema.copy()

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
                        operation["servers"].append({"url": url})

        return prepared_schema

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
        """Main method for merging schemas"""
        if not self.schemas:
            return {}

        # Prepare schemas with servers
        for service_name, source, schema in self.schemas:
            if schema is None:
                logger.warning(f"Skipping {service_name} - schema failed to load")
                continue

            logger.info(f"=== {service_name} ===")
            logger.info(f"Adding server {source.get('url')} to schema")
            schema = self._prepare_server_for_schema(schema, url=source.get("url"))

            # If grouping is enabled, add service name to tags
            if self.grouping:
                schema = self._prepare_grouping(schema, name=service_name)

        # Merge all components
        self.merged_paths = self._merge_paths()
        self.merged_schemas = self._merge_schemas()
        merged_components = self._merge_components()

        # Build final schema
        if self.schemas:
            first_schema = self.schemas[0][
                2
            ]  # Get schema from tuple (name, source, schema)
            self.merged = first_schema.copy()

            # Set merged components
            dpath.set(self.merged, "paths", self.merged_paths)

            # Create components if they don't exist
            if "components" not in self.merged:
                dpath.set(self.merged, "components", {})

            # Add schemas
            dpath.set(self.merged, "components/schemas", self.merged_schemas)

            # Add remaining components
            for component_type, component_data in merged_components.items():
                dpath.set(self.merged, f"components/{component_type}", component_data)

            # Merge tags from all schemas
            if self.grouping:
                all_tags = []
                for service_name, source, schema in self.schemas:
                    if schema and "tags" in schema:
                        all_tags.extend(schema["tags"])
                dpath.set(self.merged, "tags", all_tags)

        # Update metadata using dpath
        dpath.set(
            self.merged,
            "info/title",
            dpath.get(config, "settings/title", default="Merged API"),
        )
        dpath.set(
            self.merged,
            "info/description",
            dpath.get(config, "settings/description", default=""),
        )
        dpath.set(
            self.merged,
            "info/version",
            dpath.get(config, "settings/version", default="1.0.0"),
        )

        return self.merged
