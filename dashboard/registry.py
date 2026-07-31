#!/usr/bin/env python3
"""Registry digest resolution.

Answers one question per image tag: *what digest does this tag point at right
now?* It speaks the OCI distribution API directly (anonymous token flow, or
basic-auth credentials from config), so nothing is ever pulled -- a check costs
one HEAD request, not a layer download.

The local side of the comparison is the container's RepoDigest, which Docker
records as the digest it pulled by. For a multi-arch tag that is the index
digest, which is exactly what the registry returns for the same tag, so the two
are directly comparable.
"""

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REGISTRY = "docker.io"
DOCKER_REGISTRY_HOST = "registry-1.docker.io"
DOCKER_AUTH_HOST = "auth.docker.io"
USER_AGENT = "container-update-dashboard/1.0 (+registry-digest-check)"

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}

_AUTH_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


class _Headers(dict):
    """Header casing varies by registry and by HTTP version -- ignore it."""

    def __init__(self, source):
        super().__init__({str(k).lower(): v for k, v in dict(source or {}).items()})

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class RegistryError(Exception):
    """A lookup failed in a way worth showing the user."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind  # error | auth | ratelimit | notfound | unsupported


class ImageRef:
    __slots__ = ("registry", "repository", "tag", "digest", "original")

    def __init__(self, registry, repository, tag, digest, original):
        self.registry = registry
        self.repository = repository
        self.tag = tag
        self.digest = digest
        self.original = original

    @property
    def is_pinned(self):
        return bool(self.digest)

    @property
    def api_host(self):
        return DOCKER_REGISTRY_HOST if self.registry == DEFAULT_REGISTRY else self.registry

    @property
    def display(self):
        repo = self.repository
        if self.registry == DEFAULT_REGISTRY and repo.startswith("library/"):
            repo = repo[len("library/"):]
        prefix = "" if self.registry == DEFAULT_REGISTRY else self.registry + "/"
        return "%s%s:%s" % (prefix, repo, self.tag)

    def __repr__(self):
        return "ImageRef(%s/%s:%s)" % (self.registry, self.repository, self.tag)


def parse_image_ref(ref):
    """Split an image reference into registry / repository / tag / digest."""
    if not ref or ref.startswith("sha256:"):
        raise RegistryError("no image reference", kind="unsupported")

    original = ref
    digest = None
    if "@" in ref:
        ref, _, digest = ref.partition("@")

    registry = DEFAULT_REGISTRY
    remainder = ref
    head, slash, tail = ref.partition("/")
    if slash and ("." in head or ":" in head or head == "localhost"):
        registry = head
        remainder = tail

    tag = None
    # A colon after the last slash is a tag; before it, it is a registry port.
    if ":" in remainder.rsplit("/", 1)[-1]:
        remainder, _, tag = remainder.rpartition(":")

    if registry == DEFAULT_REGISTRY and "/" not in remainder:
        remainder = "library/" + remainder

    if not remainder:
        raise RegistryError("cannot parse image reference %r" % original, kind="unsupported")

    return ImageRef(registry, remainder, tag or "latest", digest, original)


def local_digest_for(ref, repo_digests):
    """Pick the RepoDigest that belongs to this reference's repository."""
    if ref.digest:
        return ref.digest
    for entry in repo_digests or []:
        repo, _, digest = entry.rpartition("@")
        try:
            parsed = parse_image_ref(repo + ":latest" if ":" not in repo.rsplit("/", 1)[-1] else repo)
        except RegistryError:
            continue
        if parsed.registry == ref.registry and parsed.repository == ref.repository:
            return digest
    # Single repo digest with no ambiguity: use it rather than reporting unknown.
    if len(repo_digests or []) == 1:
        return repo_digests[0].rpartition("@")[2]
    return None


class _Cache:
    def __init__(self, path=None):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        if path and os.path.exists(path):
            try:
                with open(path) as handle:
                    self.data = json.load(handle)
            except (ValueError, OSError):
                self.data = {}

    def get(self, key, ttl):
        with self.lock:
            entry = self.data.get(key)
        if not entry:
            return None
        if time.time() - entry.get("at", 0) > ttl:
            return None
        return entry.get("value")

    def put(self, key, value):
        with self.lock:
            self.data[key] = {"at": time.time(), "value": value}
            snapshot = dict(self.data)
        if self.path:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as handle:
                    json.dump(snapshot, handle)
                os.replace(tmp, self.path)
            except OSError:
                pass


class RegistryClient:
    """Resolves tags to digests, with token, success and failure caching."""

    def __init__(self, credentials=None, cache_path=None, ttl_hours=6.0,
                 failure_ttl_minutes=20.0, insecure_registries=(), timeout=20,
                 fetch_metadata=True):
        self.credentials = credentials or {}
        self.cache = _Cache(cache_path)
        self.ttl = ttl_hours * 3600
        self.failure_ttl = failure_ttl_minutes * 60
        self.insecure = {h.lower() for h in insecure_registries}
        self.timeout = timeout
        self.fetch_metadata = fetch_metadata
        self._tokens = {}
        self._token_lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def _scheme(self, host):
        bare = host.split(":", 1)[0].lower()
        if host.lower() in self.insecure or bare in self.insecure:
            return "http"
        if bare in ("localhost", "127.0.0.1", "::1"):
            return "http"
        return "https"

    def _request(self, host, path, method="GET", accept=MANIFEST_ACCEPT, token=None,
                 extra_headers=None):
        url = "%s://%s%s" % (self._scheme(host), host, path)
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = "Bearer " + token
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirectAuthHandler())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, _Headers(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - body already consumed
                pass
            return exc.code, _Headers(exc.headers), body
        except urllib.error.URLError as exc:
            raise RegistryError("cannot reach %s: %s" % (host, exc.reason)) from exc
        except OSError as exc:
            raise RegistryError("cannot reach %s: %s" % (host, exc)) from exc

    def _auth_token(self, ref, challenge):
        params = dict(_AUTH_PARAM_RE.findall(challenge))
        realm = params.get("realm")
        if not realm:
            raise RegistryError("registry %s sent no auth realm" % ref.registry, kind="auth")
        scope = params.get("scope") or "repository:%s:pull" % ref.repository
        service = params.get("service")

        cache_key = (realm, service, scope)
        with self._token_lock:
            cached = self._tokens.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]

        query = {"scope": scope}
        if service:
            query["service"] = service
        url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(query)

        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        creds = self._credentials_for(ref.registry)
        if creds:
            raw = "%s:%s" % (creds.get("username", ""), creds.get("password", ""))
            headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()

        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RegistryError(
                    "authentication rejected by %s (private image? add credentials)" % ref.registry,
                    kind="auth",
                ) from exc
            raise RegistryError("token request to %s failed: HTTP %s" % (ref.registry, exc.code)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RegistryError("token request to %s failed: %s" % (ref.registry, exc)) from exc

        token = payload.get("token") or payload.get("access_token")
        if not token:
            raise RegistryError("registry %s returned no token" % ref.registry, kind="auth")
        lifetime = float(payload.get("expires_in") or 300)
        with self._token_lock:
            self._tokens[cache_key] = (token, time.time() + max(30.0, lifetime - 30))
        return token

    def _credentials_for(self, registry):
        for key in (registry, registry.split(":", 1)[0]):
            if key in self.credentials:
                return self.credentials[key]
        if registry == DEFAULT_REGISTRY:
            for alias in (DOCKER_REGISTRY_HOST, "index.docker.io", "hub.docker.com"):
                if alias in self.credentials:
                    return self.credentials[alias]
        return None

    def _manifest_request(self, ref, reference, method="GET", accept=MANIFEST_ACCEPT):
        path = "/v2/%s/manifests/%s" % (ref.repository, urllib.parse.quote(reference, safe=""))
        status, headers, body = self._request(ref.api_host, path, method=method, accept=accept)
        if status == 401:
            challenge = headers.get("www-authenticate") or ""
            token = self._auth_token(ref, challenge)
            status, headers, body = self._request(
                ref.api_host, path, method=method, accept=accept, token=token
            )
            return status, headers, body, token
        return status, headers, body, None

    # -- public API --------------------------------------------------------

    def resolve(self, image_ref_string, platform=None):
        """Return ``{"digest": ..., "remote_created": ..., ...}`` for a tag."""
        ref = parse_image_ref(image_ref_string)
        key = "%s|%s|%s|%s" % (ref.api_host, ref.repository, ref.tag, platform or "")

        cached = self.cache.get(key, self.ttl)
        if cached is not None and not cached.get("error"):
            return dict(cached, cached=True)
        cached_failure = self.cache.get(key, self.failure_ttl)
        if cached_failure is not None and cached_failure.get("error"):
            return dict(cached_failure, cached=True)

        try:
            result = self._resolve_uncached(ref, platform)
        except RegistryError as exc:
            result = {"digest": None, "error": str(exc), "error_kind": exc.kind}
        result["registry"] = ref.registry
        result["repository"] = ref.repository
        result["tag"] = ref.tag
        self.cache.put(key, result)
        return dict(result, cached=False)

    def _resolve_uncached(self, ref, platform):
        status, headers, body, token = self._manifest_request(ref, ref.tag, method="HEAD")

        # Some registries and proxies mishandle HEAD on manifests.
        if status in (405, 501) or (status == 200 and not _digest_header(headers)):
            status, headers, body, token = self._manifest_request(ref, ref.tag, method="GET")

        if status == 404:
            raise RegistryError(
                "tag %s not found in %s" % (ref.tag, ref.registry), kind="notfound"
            )
        if status == 429:
            raise RegistryError("rate limited by %s" % ref.registry, kind="ratelimit")
        if status in (401, 403):
            raise RegistryError(
                "not authorised for %s (private image? add credentials)" % ref.repository,
                kind="auth",
            )
        if status >= 400:
            raise RegistryError("%s returned HTTP %s" % (ref.registry, status))

        digest = _digest_header(headers)
        if not digest:
            if not body:
                status, headers, body, token = self._manifest_request(ref, ref.tag, method="GET")
            digest = "sha256:" + hashlib.sha256(body).hexdigest() if body else None
        if not digest:
            raise RegistryError("%s returned no manifest digest" % ref.registry)

        result = {"digest": digest, "error": None, "error_kind": None}
        if self.fetch_metadata:
            try:
                meta = self._image_metadata(ref, digest, platform, token)
                result.update(meta)
            except (RegistryError, ValueError, KeyError, OSError):
                pass  # Metadata is a nicety; the digest is the answer.
        return result

    def _image_metadata(self, ref, digest, platform, token):
        """Follow index -> manifest -> config to read the image's build date."""
        status, headers, body, token = self._manifest_request(ref, digest, method="GET")
        if status >= 400 or not body:
            return {}
        manifest = json.loads(body.decode("utf-8"))
        media_type = manifest.get("mediaType") or headers.get("Content-Type", "")

        if media_type in INDEX_TYPES or "manifests" in manifest:
            chosen = _pick_platform(manifest.get("manifests") or [], platform)
            if not chosen:
                return {}
            status, headers, body, token = self._manifest_request(
                ref, chosen["digest"], method="GET"
            )
            if status >= 400 or not body:
                return {}
            manifest = json.loads(body.decode("utf-8"))

        config = manifest.get("config") or {}
        config_digest = config.get("digest")
        if not config_digest:
            return {}
        path = "/v2/%s/blobs/%s" % (ref.repository, config_digest)
        status, _, blob = self._request(
            ref.api_host, path, accept=config.get("mediaType", "application/json"), token=token
        )
        if status >= 400 or not blob:
            return {}
        payload = json.loads(blob.decode("utf-8"))
        return {
            "remote_created": payload.get("created"),
            "remote_platform": "%s/%s" % (payload.get("os", "?"), payload.get("architecture", "?")),
        }


class _NoRedirectAuthHandler(urllib.request.HTTPRedirectHandler):
    """Keep the Authorization header off cross-host blob redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlparse(req.full_url).netloc
            if urllib.parse.urlparse(newurl).netloc != old_host:
                new.headers = {
                    k: v for k, v in new.headers.items() if k.lower() != "authorization"
                }
        return new


def _digest_header(headers):
    for key in ("docker-content-digest", "etag"):
        value = headers.get(key)
        if value:
            value = value.strip('"')
            if value.startswith("sha256:"):
                return value
    return None


def _pick_platform(manifests, platform):
    wanted_os, _, wanted_arch = (platform or "linux/amd64").partition("/")
    fallbacks = []
    for entry in manifests:
        plat = entry.get("platform") or {}
        if plat.get("architecture") == "unknown" or plat.get("os") == "unknown":
            continue  # attestation manifests
        if plat.get("os") == wanted_os and plat.get("architecture") == wanted_arch:
            return entry
        fallbacks.append(entry)
    return fallbacks[0] if fallbacks else None


def normalise_arch(arch):
    """Map a Docker ``info.Architecture`` value onto an OCI platform string."""
    mapping = {
        "x86_64": "linux/amd64",
        "amd64": "linux/amd64",
        "aarch64": "linux/arm64",
        "arm64": "linux/arm64",
        "armv7l": "linux/arm/v7",
        "armv6l": "linux/arm/v6",
    }
    return mapping.get((arch or "").lower(), "linux/amd64")
