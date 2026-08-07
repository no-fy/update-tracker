#!/usr/bin/env python3
"""Configuration loading and saving.

One JSON file holds everything: dashboard settings, registry credentials and
the list of hosts. It contains agent tokens, so it is created and rewritten
with 0600 permissions.
"""

import copy
import hashlib
import hmac
import json
import os
import secrets
import tempfile

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ROUNDS = 240000

DEFAULT_CONFIG = {
    "dashboard": {
        "bind": "0.0.0.0",
        "port": 8500,
        "username": None,
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
    "stack_templates": [],
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
    # deepcopy, not dict(): DEFAULT_CONFIG's nested "hosts" list would otherwise
    # be shared by every config loaded in the process, so registering a host
    # against one config would quietly appear in the next one loaded.
    out = copy.deepcopy(base)
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


def hash_password(password, rounds=PASSWORD_ROUNDS, salt=None):
    """Hash a password for storage in config.json.

    The setup flow writes hashes; a plaintext `dashboard.password` still works,
    because it is documented and people hand-edit this file.
    """
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), rounds)
    return "%s$%d$%s$%s" % (PASSWORD_SCHEME, rounds, salt, digest.hex())


def verify_password(stored, supplied):
    if not stored or supplied is None:
        return False
    if stored.startswith(PASSWORD_SCHEME + "$"):
        try:
            _, rounds, salt, digest = stored.split("$", 3)
            rounds = int(rounds)
        except ValueError:
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", supplied.encode("utf-8"), salt.encode("ascii"), rounds
        )
        return hmac.compare_digest(computed.hex(), digest)
    return hmac.compare_digest(stored, supplied)


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


def upsert_stack_template(config, name, compose):
    """Save/overwrite a reusable compose snippet under `name`. Small and
    textual, so config.json is enough -- no need for anything else."""
    templates = config.setdefault("stack_templates", [])
    entry = {"name": name, "compose": compose}
    for index, existing in enumerate(templates):
        if existing.get("name") == name:
            templates[index] = entry
            return entry
    templates.append(entry)
    return entry


def remove_stack_template(config, name):
    templates = config.setdefault("stack_templates", [])
    for index, existing in enumerate(templates):
        if existing.get("name") == name:
            return templates.pop(index)
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
