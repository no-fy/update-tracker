#!/usr/bin/env python3
"""Storage for the SSH private keys used to provision remote hosts.

Keys live in their own directory beside config.json -- the directory 0700, each
key file 0600. They are never written into config.json and never returned by
the API; the only thing that ever reads them back is `ssh -i`.

A key is only needed to *install* an agent. Once a host is registered the
dashboard talks to it over HTTP with a bearer token, so keeping the key is
optional and off by default.
"""

import os
import re
import tempfile

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def default_key_dir(config_path=None):
    from . import config as config_mod

    base = os.path.dirname(config_path or config_mod.default_config_path())
    return os.path.join(base, "keys")


def slugify(name):
    """A filesystem-safe stem. Never returns something that escapes the dir."""
    slug = SAFE_NAME.sub("-", (name or "").strip()).strip(".-")
    return slug[:64] or "key"


class KeyStore:
    def __init__(self, directory):
        self.directory = directory

    def _ensure_dir(self):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    def path_for(self, name):
        return os.path.join(self.directory, slugify(name) + ".key")

    def save(self, name, material):
        """Write a private key, 0600, and return its path."""
        if not material or not material.strip():
            raise ValueError("empty key material")
        self._ensure_dir()
        path = self.path_for(name)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".key-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(material.strip() + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path

    def delete(self, name):
        path = self.path_for(name)
        try:
            os.unlink(path)
            return True
        except OSError:
            return False

    def has(self, name):
        return os.path.exists(self.path_for(name))

    def names(self):
        try:
            entries = os.listdir(self.directory)
        except OSError:
            return []
        return sorted(e[: -len(".key")] for e in entries if e.endswith(".key"))
