#!/usr/bin/env python3
"""Pending OS package updates, read straight off the package manager's own files.

Nothing is executed and nothing is installed: the databases every package
manager already keeps on disk are parsed directly. That keeps this in line with
the rest of the tool -- read-only, no privileges -- and means it works from a
container given a read-only bind mount of the host's root filesystem.

    docker run … -v /:/host:ro -e CUD_HOST_ROOT=/host …

Supported: dpkg/apt (Debian, Ubuntu, Raspberry Pi OS, Proxmox), apk (Alpine),
pacman (Arch). An rpm host is detected and reported as unsupported rather than
quietly returning "no updates", which would be the dangerous answer.

What "available" means depends on the host having refreshed its package lists
(`apt update` and friends). We report how stale that data is rather than
pretending freshness we cannot provide.
"""

import bz2
import gzip
import io
import lzma
import os
import re
import tarfile
import time

# How a pending update is ranked. Security first, because that is the only
# distinction most people act on.
SEVERITY_ORDER = ["security", "important", "routine"]

# Packages where an update means a reboot or a service interruption, so they
# deserve to be called out even when the update is not a security fix.
IMPORTANT_PATTERNS = re.compile(
    r"^(linux-image|linux-headers|linux-generic|linux-firmware|kernel|"
    r"systemd|libc6|libc-bin|glibc|musl|openssl|libssl|openssh|sshd?|"
    r"docker(-ce)?|containerd|dbus|grub)",
    re.IGNORECASE,
)


def _root(path, host_root=None):
    root = host_root or os.environ.get("CUD_HOST_ROOT") or "/"
    return os.path.join(root, path.lstrip("/"))


# ------------------------------------------------------------------- lz4 --
#
# Debian keeps /var/lib/apt/lists/*_Packages compressed with lz4, which the
# standard library does not implement. Reading them matters more than the
# purity of not implementing a codec: without this, a Debian or Proxmox host
# reports zero updates while apt sees dozens -- the one wrong answer that
# actually costs someone something.

LZ4_MAGIC = b"\x04\x22\x4d\x18"


def _lz4_block(src, out):
    """Decode one LZ4 block, appending into `out` (a bytearray)."""
    index = 0
    length = len(src)
    while index < length:
        token = src[index]
        index += 1

        literals = token >> 4
        if literals == 15:
            while True:
                step = src[index]
                index += 1
                literals += step
                if step != 255:
                    break
        if literals:
            out += src[index:index + literals]
            index += literals
        if index >= length:
            break

        offset = src[index] | (src[index + 1] << 8)
        index += 2
        if offset == 0:
            raise ValueError("lz4: zero offset")

        match = token & 0x0F
        if match == 15:
            while True:
                step = src[index]
                index += 1
                match += step
                if step != 255:
                    break
        match += 4

        start = len(out) - offset
        if start < 0:
            raise ValueError("lz4: offset before start of output")
        if offset >= match:
            out += out[start:start + match]      # non-overlapping: one slice
        else:
            for position in range(match):        # overlapping run
                out.append(out[start + position])
    return out


def lz4_decompress(data):
    """Decode an LZ4 *frame* (the file format), not a bare block."""
    if data[:4] != LZ4_MAGIC:
        raise ValueError("not an lz4 frame")
    flg = data[4]
    if flg >> 6 != 1:
        raise ValueError("unsupported lz4 version")
    has_content_size = (flg >> 3) & 1
    has_content_checksum = (flg >> 2) & 1
    has_dict_id = flg & 1
    has_block_checksum = (flg >> 4) & 1

    position = 6                       # magic(4) + FLG + BD
    position += 8 if has_content_size else 0
    position += 4 if has_dict_id else 0
    position += 1                      # header checksum

    out = bytearray()
    total = len(data)
    while position + 4 <= total:
        size = int.from_bytes(data[position:position + 4], "little")
        position += 4
        if size == 0:                  # end mark
            break
        stored_raw = bool(size & 0x80000000)
        size &= 0x7FFFFFFF
        block = data[position:position + size]
        position += size
        if stored_raw:
            out += block
        else:
            _lz4_block(block, out)
        if has_block_checksum:
            position += 4
    if has_content_checksum:
        position += 4
    return bytes(out)


def _read_index(path):
    """Read an apt index whatever apt chose to compress it with."""
    if path.endswith(".lz4"):
        with open(path, "rb") as handle:
            return lz4_decompress(handle.read()).decode("utf-8", "replace")
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    if path.endswith(".xz"):
        with lzma.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    if path.endswith(".bz2"):
        with bz2.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


# ---------------------------------------------------------------- versions --


def _split_debian_version(version):
    epoch = "0"
    rest = version
    if ":" in rest:
        epoch, _, rest = rest.partition(":")
    upstream, _, revision = rest.rpartition("-")
    if not upstream:  # no revision at all
        upstream, revision = rest, ""
    return epoch, upstream, revision


def _order(char):
    """Debian's collation: ~ sorts before everything, letters before symbols."""
    if char.isdigit():
        return 0
    if char.isalpha():
        return ord(char)
    if char == "~":
        return -1
    return ord(char) + 256


def _compare_fragment(left, right):
    i = j = 0
    while i < len(left) or j < len(right):
        first_diff = 0
        while (i < len(left) and not left[i].isdigit()) or \
              (j < len(right) and not right[j].isdigit()):
            ac = _order(left[i]) if i < len(left) else 0
            bc = _order(right[j]) if j < len(right) else 0
            if ac != bc:
                return -1 if ac < bc else 1
            i += 1
            j += 1
        while i < len(left) and left[i] == "0":
            i += 1
        while j < len(right) and right[j] == "0":
            j += 1
        while i < len(left) and left[i].isdigit() and j < len(right) and right[j].isdigit():
            if not first_diff:
                first_diff = (ord(left[i]) - ord(right[j]))
            i += 1
            j += 1
        if i < len(left) and left[i].isdigit():
            return 1
        if j < len(right) and right[j].isdigit():
            return -1
        if first_diff:
            return -1 if first_diff < 0 else 1
    return 0


def compare_versions(left, right):
    """Debian version ordering. Also close enough for pacman and apk.

    Returns -1, 0 or 1. This is the piece that decides whether something is an
    update at all, so it follows the documented algorithm rather than a
    string compare that would call 1.10 older than 1.9.
    """
    if left == right:
        return 0
    le, lu, lr = _split_debian_version(left)
    re_, ru, rr = _split_debian_version(right)
    try:
        result = (int(le) > int(re_)) - (int(le) < int(re_))
    except ValueError:
        result = 0
    if result:
        return result
    result = _compare_fragment(lu, ru)
    if result:
        return result
    return _compare_fragment(lr, rr)


# ------------------------------------------------------------ classification --


def classify(name, origin_hint="", installed="", candidate=""):
    """How much someone should care about this particular update."""
    hint = (origin_hint or "").lower()
    if "security" in hint:
        return "security"
    if IMPORTANT_PATTERNS.match(name or ""):
        return "important"
    return "routine"


def _stanzas(text):
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields = {}
        key = None
        for line in block.split("\n"):
            if line.startswith((" ", "\t")) and key:
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        yield fields


# -------------------------------------------------------------------- dpkg --


def _dpkg_installed(host_root=None):
    path = _root("/var/lib/dpkg/status", host_root)
    installed = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for fields in _stanzas(handle.read()):
            status = fields.get("Status", "")
            if "installed" not in status or "not-installed" in status:
                continue
            name = fields.get("Package")
            if name:
                installed[name] = fields.get("Version", "")
    return installed


def _apt_candidates(host_root=None):
    """Best available version per package, plus where it came from.

    apt encodes the suite in the list filename, e.g.
    ..._debian-security_dists_bookworm-security_main_binary-amd64_Packages,
    which is exactly the signal needed to tell a security update apart.
    """
    lists_dir = _root("/var/lib/apt/lists", host_root)
    candidates = {}
    newest_mtime = 0
    if not os.path.isdir(lists_dir):
        return candidates, newest_mtime

    for entry in sorted(os.listdir(lists_dir)):
        stem = entry
        for suffix in (".lz4", ".gz", ".xz", ".bz2"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        if not stem.endswith("_Packages"):
            continue
        full = os.path.join(lists_dir, entry)
        try:
            newest_mtime = max(newest_mtime, os.path.getmtime(full))
            text = _read_index(full)
        except (OSError, ValueError):
            continue

        for fields in _stanzas(text):
            name = fields.get("Package")
            version = fields.get("Version")
            if not name or not version:
                continue
            record = {
                "version": version,
                "origin": entry,
                "description": (fields.get("Description") or "").strip(),
                "section": fields.get("Section") or "",
                "priority": fields.get("Priority") or "",
                "size": _int_or_none(fields.get("Size")),
                "installed_size": _int_or_none(fields.get("Installed-Size")),
                "homepage": fields.get("Homepage") or "",
                "source": fields.get("Source") or name,
                "architecture": fields.get("Architecture") or "",
            }
            known = candidates.get(name)
            if known is None or compare_versions(version, known["version"]) > 0:
                candidates[name] = record
            elif compare_versions(version, known["version"]) == 0 \
                    and "security" in entry.lower() \
                    and "security" not in known["origin"].lower():
                # The same version is usually published to both -security and
                # -updates. Whichever filename sorted first must not decide
                # whether this reads as a security fix.
                candidates[name] = record
    return candidates, newest_mtime


def scan_dpkg(host_root=None):
    installed = _dpkg_installed(host_root)
    candidates, mtime = _apt_candidates(host_root)
    updates = []
    for name, current in installed.items():
        candidate = candidates.get(name)
        if not candidate:
            continue
        if compare_versions(candidate["version"], current) > 0:
            updates.append({
                "name": name,
                "installed": current,
                "candidate": candidate["version"],
                "severity": classify(name, candidate["origin"], current, candidate["version"]),
                "source": _pretty_origin(candidate["origin"]),
                "description": candidate.get("description") or "",
                "section": candidate.get("section") or "",
                "priority": candidate.get("priority") or "",
                "download_bytes": candidate.get("size"),
                "installed_bytes": (candidate.get("installed_size") or 0) * 1024 or None,
                "homepage": candidate.get("homepage") or "",
                "source_package": candidate.get("source") or name,
                "architecture": candidate.get("architecture") or "",
            })
    return {
        "manager": "apt",
        "supported": True,
        "updates": updates,
        "lists_updated": mtime or None,
        "reboot_required": os.path.exists(_root("/var/run/reboot-required", host_root)),
        "packages_installed": len(installed),
    }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pretty_origin(entry):
    """Turn an apt list filename into something a person can read."""
    stem = entry[:-len("_Packages")] if entry.endswith("_Packages") else entry
    stem = stem[:-3] if stem.endswith(".gz") else stem
    parts = stem.split("_dists_")
    if len(parts) == 2:
        host = parts[0].split("_")[0]
        suite = parts[1].split("_")[0]
        return "%s %s" % (host, suite)
    return stem


# --------------------------------------------------------------------- apk --


def _apk_index_entries(text):
    """APKINDEX stanzas are single letters: P=name, V=version."""
    for block in text.split("\n\n"):
        name = version = None
        for line in block.split("\n"):
            if line.startswith("P:"):
                name = line[2:].strip()
            elif line.startswith("V:"):
                version = line[2:].strip()
        if name and version:
            yield name, version


def scan_apk(host_root=None):
    installed_path = _root("/lib/apk/db/installed", host_root)
    installed = {}
    with open(installed_path, "r", encoding="utf-8", errors="replace") as handle:
        for name, version in _apk_index_entries(handle.read()):
            installed[name] = version

    cache_dir = _root("/var/cache/apk", host_root)
    candidates = {}
    newest_mtime = 0
    if os.path.isdir(cache_dir):
        for entry in sorted(os.listdir(cache_dir)):
            if not entry.startswith("APKINDEX"):
                continue
            full = os.path.join(cache_dir, entry)
            try:
                newest_mtime = max(newest_mtime, os.path.getmtime(full))
                with tarfile.open(full, "r:*") as archive:
                    member = archive.extractfile("APKINDEX")
                    text = member.read().decode("utf-8", "replace") if member else ""
            except (OSError, tarfile.TarError, KeyError):
                continue
            for name, version in _apk_index_entries(text):
                known = candidates.get(name)
                if known is None or compare_versions(version, known) > 0:
                    candidates[name] = version

    updates = []
    for name, current in installed.items():
        candidate = candidates.get(name)
        if candidate and compare_versions(candidate, current) > 0:
            updates.append({
                "name": name,
                "installed": current,
                "candidate": candidate,
                # Alpine ships security fixes in the normal repos, so there is
                # no separate suite to key off; only the package name tells us
                # anything.
                "severity": classify(name, "", current, candidate),
                "source": "alpine",
            })
    return {
        "manager": "apk",
        "supported": True,
        "updates": updates,
        "lists_updated": newest_mtime or None,
        "reboot_required": False,
        "packages_installed": len(installed),
    }


# ------------------------------------------------------------------ pacman --


def _pacman_desc_version(text):
    lines = text.split("\n")
    name = version = None
    for index, line in enumerate(lines):
        if line.strip() == "%NAME%" and index + 1 < len(lines):
            name = lines[index + 1].strip()
        elif line.strip() == "%VERSION%" and index + 1 < len(lines):
            version = lines[index + 1].strip()
    return name, version


def scan_pacman(host_root=None):
    local_dir = _root("/var/lib/pacman/local", host_root)
    installed = {}
    for entry in sorted(os.listdir(local_dir)):
        desc = os.path.join(local_dir, entry, "desc")
        try:
            with open(desc, "r", encoding="utf-8", errors="replace") as handle:
                name, version = _pacman_desc_version(handle.read())
        except OSError:
            continue
        if name and version:
            installed[name] = version

    sync_dir = _root("/var/lib/pacman/sync", host_root)
    candidates = {}
    newest_mtime = 0
    if os.path.isdir(sync_dir):
        for entry in sorted(os.listdir(sync_dir)):
            if not entry.endswith(".db"):
                continue
            full = os.path.join(sync_dir, entry)
            try:
                newest_mtime = max(newest_mtime, os.path.getmtime(full))
                with tarfile.open(full, "r:*") as archive:
                    for member in archive.getmembers():
                        if not member.name.endswith("/desc"):
                            continue
                        handle = archive.extractfile(member)
                        if not handle:
                            continue
                        name, version = _pacman_desc_version(
                            handle.read().decode("utf-8", "replace"))
                        if not name or not version:
                            continue
                        known = candidates.get(name)
                        if known is None or compare_versions(version, known) > 0:
                            candidates[name] = version
            except (OSError, tarfile.TarError):
                continue

    updates = []
    for name, current in installed.items():
        candidate = candidates.get(name)
        if candidate and compare_versions(candidate, current) > 0:
            updates.append({
                "name": name,
                "installed": current,
                "candidate": candidate,
                "severity": classify(name, "", current, candidate),
                "source": "arch",
            })
    return {
        "manager": "pacman",
        "supported": True,
        "updates": updates,
        "lists_updated": newest_mtime or None,
        "reboot_required": False,
        "packages_installed": len(installed),
    }


# ---------------------------------------------------------------- dispatch --


def detect_manager(host_root=None):
    if os.path.exists(_root("/var/lib/dpkg/status", host_root)):
        return "apt"
    if os.path.exists(_root("/lib/apk/db/installed", host_root)):
        return "apk"
    if os.path.isdir(_root("/var/lib/pacman/local", host_root)):
        return "pacman"
    for probe in ("/var/lib/rpm/rpmdb.sqlite", "/var/lib/rpm/Packages", "/usr/bin/dnf"):
        if os.path.exists(_root(probe, host_root)):
            return "rpm"
    return None


def summarise(updates):
    counts = {level: 0 for level in SEVERITY_ORDER}
    for update in updates:
        counts[update.get("severity", "routine")] = \
            counts.get(update.get("severity", "routine"), 0) + 1
    return counts


def collect(host_root=None):
    """Everything the dashboard needs about pending OS updates on this host."""
    root = host_root or os.environ.get("CUD_HOST_ROOT") or "/"
    result = {
        "available": False,
        "manager": None,
        "supported": False,
        "updates": [],
        "counts": {level: 0 for level in SEVERITY_ORDER},
        "reboot_required": False,
        "error": None,
        "host_root": root,
        "checked_at": time.time(),
    }

    manager = detect_manager(host_root)
    if manager is None:
        result["error"] = (
            "no package manager found under %s -- mount the host filesystem "
            "read-only and set CUD_HOST_ROOT to see OS updates" % root
        )
        return result

    result["manager"] = manager
    if manager == "rpm":
        result["error"] = (
            "rpm-based hosts are detected but not read yet: the rpm database is "
            "a binary format this agent cannot parse without librpm"
        )
        return result

    scanner = {"apt": scan_dpkg, "apk": scan_apk, "pacman": scan_pacman}[manager]
    try:
        scanned = scanner(host_root)
    except OSError as exc:
        result["error"] = "cannot read the %s database: %s" % (manager, exc)
        return result

    scanned["updates"].sort(
        key=lambda u: (SEVERITY_ORDER.index(u["severity"]), u["name"])
    )
    result.update(scanned)
    result["available"] = True
    result["counts"] = summarise(scanned["updates"])
    return result
