#!/usr/bin/env python3
"""Configuration loading and saving.

One JSON file holds everything: dashboard settings, registry credentials and
the list of hosts. It contains agent tokens, so it is created and rewritten
with 0600 permissions.
"""

import json
import os
import tempfile

DEFAULT_CONFIG = {
    "dashboard": {
        "bind": "0.0.0.0",
        "port": 8500,
        "refresh_interval_minutes": 30,
        "registry_cache_hours": 6,
        "registry_failure_cache_minutes": 20,
        "poll_timeout_seconds": 20,
        "max_parallel_hosts": 8,
        "max_parallel_registry_lookups": 6,
        "include_stopped": True,
        "fetch_remote_metadata": True,
    },
    "registries": {},
    "insecure_registries": [],
    "hosts": [],
}


def default_config_path():
    env = os.environ.get("CUD_CONFIG")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "config", "config.json")


def default_cache_path(config_path=None):
    """The registry cache lives beside whichever config file is in use."""
    return os.path.join(
        os.path.dirname(config_path or default_config_path()), "registry-cache.json"
    )


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path=None):
    path = path or default_config_path()
    if not os.path.exists(path):
        return _merge(DEFAULT_CONFIG, {}), path
    with open(path) as handle:
        raw = json.load(handle)
    return _merge(DEFAULT_CONFIG, raw), path


def save_config(config, path=None):
    path = path or default_config_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(config, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def find_host(config, name):
    for host in config.get("hosts", []):
        if host.get("name") == name:
            return host
    return None


def upsert_host(config, host):
    hosts = config.setdefault("hosts", [])
    for index, existing in enumerate(hosts):
        if existing.get("name") == host.get("name"):
            hosts[index] = _merge(existing, host)
            return hosts[index], False
    hosts.append(host)
    return host, True


def remove_host(config, name):
    hosts = config.setdefault("hosts", [])
    for index, existing in enumerate(hosts):
        if existing.get("name") == name:
            return hosts.pop(index)
    return None


def ensure_local_host(config):
    """Add the machine the dashboard runs on, if no local host is configured."""
    for host in config.get("hosts", []):
        if host.get("mode") == "local":
            return host, False
    host = {
        "name": "local",
        "mode": "local",
        "label": "This machine",
        "docker_socket": os.environ.get("DOCKER_HOST") or "/var/run/docker.sock",
        "enabled": True,
    }
    config.setdefault("hosts", []).insert(0, host)
    return host, True
