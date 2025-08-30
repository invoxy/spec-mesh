use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3::wrap_pyfunction;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::HashMap;

// === Типы ===
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Source {
    name: String,
    schema: String,
    url: String,
    enabled: bool,
}

// === Вспомогательные функции ===

#[pyfunction]
fn safe_name(name: &str) -> String {
    let re = Regex::new(r"[^a-zA-Z0-9_-]").unwrap();
    let mut name = re.replace_all(name, "_").to_string();
    let re2 = Regex::new(r"_+").unwrap();
    name = re2.replace_all(&name, "_").to_string();
    name = name.trim_matches('_').to_string();
    name.to_lowercase()
}

fn safe_name_internal(name: &str) -> String {
    let re = Regex::new(r"[^a-zA-Z0-9_-]").unwrap();
    let mut name = re.replace_all(name, "_").to_string();
    let re2 = Regex::new(r"_+").unwrap();
    name = re2.replace_all(&name, "_").to_string();
    name = name.trim_matches('_').to_string();
    name.to_lowercase()
}

#[pyfunction]
fn is_caddy_available() -> bool {
    // Упрощённая проверка: попробуем подключиться к localhost:80
    use std::net::TcpStream;
    use std::time::Duration;

    if std::env::var("CADDY_AVAILABLE").unwrap_or_default() == "true" {
        return true;
    }

    if let Ok(stream) =
        TcpStream::connect_timeout(&"127.0.0.1:80".parse().unwrap(), Duration::from_secs(2))
    {
        drop(stream);
        return true;
    }

    false
}

// === Основные функции ===

#[pyfunction]
fn get_schema_sync(url: &str) -> PyResult<PyObject> {
    let rt = tokio::runtime::Runtime::new().unwrap();
    let result = rt.block_on(async {
        let client = reqwest::Client::new();
        let response = client.get(url).send().await.map_err(|e| e.to_string())?;
        let content_type = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_lowercase();

        let text = response.text().await.map_err(|e| e.to_string())?;

        let value = if content_type.contains("vnd.oai.openapi") || content_type.contains("json") {
            serde_json::from_str(&text)
                .or_else(|_| serde_yaml::from_str(&text))
                .map_err(|e| e.to_string())?
        } else if content_type.contains("yaml") || content_type.contains("yml") {
            serde_yaml::from_str(&text).map_err(|e| e.to_string())?
        } else {
            // Попробуем JSON как fallback
            serde_json::from_str(&text)
                .or_else(|_| serde_yaml::from_str(&text))
                .map_err(|e| e.to_string())?
        };

        Ok::<Value, String>(value)
    });

    match result {
        Ok(value) => Python::with_gil(|py| {
            let json_str = serde_json::to_string(&value).unwrap();
            let py_dict = PyDict::new(py);
            py_dict.set_item("data", json_str).unwrap();
            Ok(py_dict.into_py(py))
        }),
        Err(e) => Err(PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e)),
    }
}

#[pyfunction]
fn get_schemas_sync(sources: &PyList, enabled: bool) -> PyResult<Vec<PyObject>> {
    let mut results = Vec::new();

    Python::with_gil(|py| {
        for item in sources.iter() {
            let dict = item.downcast::<PyDict>().map_err(|_| {
                PyErr::new::<pyo3::exceptions::PyValueError, _>("Each source must be a dict")
            })?;

            let name: String = dict
                .get_item("name")
                .and_then(|v| v.extract().ok())
                .unwrap_or_else(|| uuid::Uuid::new_v4().to_string()[..10].to_string());

            let schema_url: String = match dict.get_item("schema") {
                Some(v) => v.extract()?,
                None => continue,
            };

            let service_url: String = dict
                .get_item("url")
                .and_then(|v| v.extract().ok())
                .unwrap_or_else(|| "http://localhost".to_string());

            let enabled_flag: bool = dict
                .get_item("enabled")
                .and_then(|v| v.extract().ok())
                .unwrap_or(enabled);

            if enabled_flag {
                // Создаем результат для каждого источника
                let result = PyDict::new(py);
                result.set_item("name", name)?;
                result.set_item("url", service_url)?;
                result.set_item("schema", schema_url.clone())?;
                result.set_item("enabled", enabled_flag)?;

                // Получаем схему
                let schema = get_schema_sync(&schema_url)?;
                result.set_item("schema_data", schema)?;

                results.push(result.into_py(py));
            }
        }
        Ok(results)
    })
}

#[pyfunction]
fn merge_schemas_sync(schemas: &PyList, grouping: bool) -> PyResult<PyObject> {
    if schemas.len() == 0 {
        return Python::with_gil(|py| {
            let empty_dict = PyDict::new(py);
            Ok(empty_dict.into_py(py))
        });
    }

    let mut merged_paths: HashMap<String, Value> = HashMap::new();
    let mut merged_schemas: HashMap<String, Value> = HashMap::new();
    let mut merged_components: HashMap<String, HashMap<String, Value>> = HashMap::new();
    let mut all_tags: Vec<Value> = Vec::new();

    // Обрабатываем каждую схему
    for schema_item in schemas.iter() {
        let dict = schema_item.downcast::<PyDict>().map_err(|_| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>("Each schema must be a dict")
        })?;

        let name: String = dict.get_item("name").unwrap().extract()?;
        let schema_data = dict.get_item("schema_data").unwrap();

        // Конвертируем Python объект в serde_json::Value
        let schema: Value = serde_json::from_str(&schema_data.to_string()).map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                "Failed to parse schema: {}",
                e
            ))
        })?;

        if schema.is_null() {
            eprintln!("Skipping {}: schema is null", name);
            continue;
        }

        // Добавляем серверы
        let mut schema_with_servers = add_servers_to_schema(
            &schema,
            &dict.get_item("url").unwrap().extract::<String>()?,
            &name,
        );

        // Группировка: добавляем имя сервиса к тегам
        if grouping {
            add_service_prefix_to_tags(&mut schema_with_servers, &name);
            if let Some(tags) = schema_with_servers.get("tags") {
                if let Some(tags_arr) = tags.as_array() {
                    all_tags.extend(tags_arr.iter().cloned());
                }
            }
        }

        // Слияние путей
        if let Some(paths) = schema_with_servers.get("paths").and_then(|v| v.as_object()) {
            for (path, methods) in paths {
                let key = if merged_paths.contains_key(path) {
                    format!("{}_{}", path, name)
                } else {
                    path.clone()
                };
                if merged_paths.contains_key(&key) {
                    eprintln!("Path conflict: {} -> {}", path, key);
                }
                merged_paths.insert(key, methods.clone());
            }
        }

        // Слияние схем
        if let Some(schemas_obj) = schema_with_servers
            .get("components")
            .and_then(|c| c.get("schemas"))
            .and_then(|s| s.as_object())
        {
            for (schema_name, def) in schemas_obj {
                let key = if merged_schemas.contains_key(schema_name) {
                    format!("{}_{}", schema_name, name)
                } else {
                    schema_name.clone()
                };
                if merged_schemas.contains_key(&key) {
                    eprintln!("Schema conflict: {} -> {}", schema_name, key);
                }
                merged_schemas.insert(key, def.clone());
            }
        }

        // Остальные компоненты
        if let Some(components) = schema_with_servers
            .get("components")
            .and_then(|c| c.as_object())
        {
            for (ctype, data) in components {
                if ctype == "schemas" {
                    continue;
                }
                let map = merged_components
                    .entry(ctype.clone())
                    .or_insert_with(HashMap::new);
                if let Some(obj) = data.as_object() {
                    for (comp_name, def) in obj {
                        let key = if map.contains_key(comp_name) {
                            format!("{}_{}", comp_name, name)
                        } else {
                            comp_name.clone()
                        };
                        if map.contains_key(&key) {
                            eprintln!("Component conflict: {}/{} -> {}", ctype, comp_name, key);
                        }
                        map.insert(key, def.clone());
                    }
                }
            }
        }
    }

    // Формируем итоговую схему
    let mut merged = json!({
        "info": {
            "title": "Merged API",
            "description": "",
            "version": "1.0.0"
        }
    });

    merged["paths"] = Value::Object(merged_paths.into_iter().collect::<Map<String, Value>>());

    let mut components = Map::new();
    components.insert(
        "schemas".to_string(),
        Value::Object(merged_schemas.into_iter().collect::<Map<String, Value>>()),
    );

    for (ctype, data) in merged_components {
        components.insert(
            ctype,
            Value::Object(data.into_iter().collect::<Map<String, Value>>()),
        );
    }

    merged["components"] = Value::Object(components);

    if grouping {
        merged["tags"] = Value::Array(all_tags);
    }

    Python::with_gil(|py| {
        let json_str = serde_json::to_string(&merged).unwrap();
        let py_dict = PyDict::new(py);
        py_dict.set_item("merged_schema", json_str).unwrap();
        Ok(py_dict.into_py(py))
    })
}

fn add_servers_to_schema(schema: &Value, url: &str, service_name: &str) -> Value {
    let mut schema = schema.clone();
    if let Some(paths) = schema.get_mut("paths").and_then(|v| v.as_object_mut()) {
        for operations in paths.values_mut() {
            if let Some(methods) = operations.as_object_mut() {
                for operation in methods.values_mut() {
                    if let Some(op) = operation.as_object_mut() {
                        let servers = op.entry("servers").or_insert_with(|| json!([]));
                        if let Some(servers_arr) = servers.as_array_mut() {
                            if !servers_arr
                                .iter()
                                .any(|s| s.get("url").and_then(|u| u.as_str()) == Some(url))
                            {
                                let mut server_obj = json!({"url": url});
                                let proxy_enabled =
                                    std::env::var("PROXY_ENABLED").unwrap_or_default() == "true";

                                if proxy_enabled && is_caddy_available() {
                                    let safe = safe_name_internal(service_name);
                                    let proxy_url = format!("/proxy/{}", safe);
                                    server_obj["url"] = json!(proxy_url);
                                    server_obj["description"] =
                                        json!(format!("Proxied to {}", url));
                                }
                                servers_arr.push(server_obj);
                            }
                        }
                    }
                }
            }
        }
    }
    schema
}

fn add_service_prefix_to_tags(schema: &mut Value, service_name: &str) {
    if let Some(tags) = schema.get_mut("tags").and_then(|v| v.as_array_mut()) {
        for tag in tags {
            if let Some(tag_obj) = tag.as_object_mut() {
                if let Some(name) = tag_obj.get("name").and_then(|n| n.as_str()) {
                    tag_obj.insert(
                        "name".to_string(),
                        json!(format!("{} | {}", service_name, name)),
                    );
                }
            }
        }
    }

    if let Some(paths) = schema.get_mut("paths").and_then(|v| v.as_object_mut()) {
        for operations in paths.values_mut() {
            if let Some(methods) = operations.as_object_mut() {
                for operation in methods.values_mut() {
                    if let Some(op) = operation.as_object_mut() {
                        if let Some(tags) = op.get_mut("tags").and_then(|t| t.as_array_mut()) {
                            for tag in tags {
                                if let Some(t) = tag.as_str() {
                                    *tag = json!(format!("{} | {}", service_name, t));
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

// === Модуль ===
#[pymodule]
fn openapi_merger(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(get_schema_sync, m)?)?;
    m.add_function(wrap_pyfunction!(get_schemas_sync, m)?)?;
    m.add_function(wrap_pyfunction!(merge_schemas_sync, m)?)?;
    m.add_function(wrap_pyfunction!(safe_name, m)?)?;
    m.add_function(wrap_pyfunction!(is_caddy_available, m)?)?;
    Ok(())
}
