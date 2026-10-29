#!/usr/bin/env python3
"""FolderSync - compare and sync several folders (local and/or SSH) from a web UI.

Usage:
    pip install paramiko
    python foldersync.py [--config foldersync.json] [--host 127.0.0.1] [--port 8765]

Then open http://127.0.0.1:8765 in a browser.
"""
import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import posixpath
import stat
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import paramiko
except ImportError:  # SSH folders will report a clear error
    paramiko = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "foldersync.json")
MTIME_TOLERANCE = 2.0  # seconds; FAT/SMB/scp can round timestamps
CHUNK = 1024 * 1024

DEFAULT_CONFIG = {
    "active": "example",
    "profiles": {
        "example": {
            "folders": [
                {"name": "Local A", "type": "local", "path": "~/projects/app"},
                {"name": "Local B", "type": "local", "path": "~/backup/app"},
                {"name": "Server", "type": "ssh", "host": "example.com", "port": 22,
                 "user": "me", "key_file": "~/.ssh/id_rsa", "password": "",
                 "path": "/home/me/app"},
            ],
            "exclude": [".git", "__pycache__", "*.pyc", ".DS_Store", "node_modules"],
            "compare": "mtime",  # "mtime" (size+mtime) or "hash" (checksum when mtime differs)
        }
    },
}


# --------------------------------------------------------------------------- config

_config_lock = threading.Lock()


def load_config():
    with _config_lock:
        if not os.path.exists(CONFIG_PATH):
            save_config(DEFAULT_CONFIG, locked=True)
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)


def save_config(cfg, locked=False):
    def _write():
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    if locked:
        _write()
    else:
        with _config_lock:
            _write()


def get_profile(name=None):
    cfg = load_config()
    name = name or cfg.get("active")
    if name not in cfg["profiles"]:
        raise ValueError("Unknown profile: %s" % name)
    return name, cfg["profiles"][name]


def is_excluded(rel, patterns):
    parts = rel.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


# --------------------------------------------------------------------------- endpoints

class LocalFolder:
    def __init__(self, spec):
        self.spec = spec
        self.root = os.path.abspath(os.path.expanduser(spec["path"]))

    def _abs(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def list(self, exclude):
        out = {}
        if not os.path.isdir(self.root):
            raise FileNotFoundError("Folder does not exist: %s" % self.root)
        for dirpath, dirnames, filenames in os.walk(self.root):
            reldir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            reldir = "" if reldir == "." else reldir
            dirnames[:] = [d for d in dirnames
                           if not is_excluded(posixpath.join(reldir, d), exclude)]
            for fn in filenames:
                rel = posixpath.join(reldir, fn)
                if is_excluded(rel, exclude):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, fn))
                except OSError:
                    continue
                out[rel] = {"size": st.st_size, "mtime": st.st_mtime}
        return out

    def open_read(self, rel):
        return open(self._abs(rel), "rb")

    def write(self, rel, src_fh, mtime):
        path = self._abs(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".foldersync.tmp"
        with open(tmp, "wb") as out:
            while True:
                buf = src_fh.read(CHUNK)
                if not buf:
                    break
                out.write(buf)
        os.replace(tmp, path)
        os.utime(path, (mtime, mtime))

    def delete(self, rel):
        os.remove(self._abs(rel))

    def hash(self, rel):
        h = hashlib.md5()
        with self.open_read(rel) as f:
            for buf in iter(lambda: f.read(CHUNK), b""):
                h.update(buf)
        return h.hexdigest()

    def close(self):
        pass


class SSHFolder:
    def __init__(self, spec):
        if paramiko is None:
            raise RuntimeError("SSH folders need paramiko: pip install paramiko")
        self.spec = spec
        self.root = spec["path"].rstrip("/") or "/"
        self.lock = threading.RLock()
        self.client = None
        self.sftp = None

    def _connect(self):
        if self.client is not None and self.client.get_transport() and \
                self.client.get_transport().is_active():
            return
        s = self.spec
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(hostname=s["host"], port=int(s.get("port") or 22),
                      username=s.get("user") or None, timeout=15)
        if s.get("key_file"):
            kwargs["key_filename"] = os.path.expanduser(s["key_file"])
        if s.get("password"):
            kwargs["password"] = s["password"]
        client.connect(**kwargs)
        self.client = client
        self.sftp = client.open_sftp()
        if self.root.startswith("~"):
            home = self.sftp.normalize(".")
            self.root = posixpath.join(home, self.root[1:].lstrip("/"))

    def _abs(self, rel):
        return posixpath.join(self.root, rel)

    def list(self, exclude):
        with self.lock:
            self._connect()
            out = {}
            stack = [""]
            while stack:
                reldir = stack.pop()
                for a in self.sftp.listdir_attr(self._abs(reldir)):
                    rel = posixpath.join(reldir, a.filename)
                    if is_excluded(rel, exclude):
                        continue
                    if stat.S_ISDIR(a.st_mode):
                        stack.append(rel)
                    elif stat.S_ISREG(a.st_mode):
                        out[rel] = {"size": a.st_size, "mtime": float(a.st_mtime)}
            return out

    def open_read(self, rel):
        with self.lock:
            self._connect()
            fh = self.sftp.open(self._abs(rel), "rb")
            fh.prefetch()
            return fh

    def _makedirs(self, path):
        parts, cur = path.strip("/").split("/"), ""
        for p in parts:
            cur += "/" + p
            try:
                self.sftp.stat(cur)
            except IOError:
                self.sftp.mkdir(cur)

    def write(self, rel, src_fh, mtime):
        with self.lock:
            self._connect()
            path = self._abs(rel)
            self._makedirs(posixpath.dirname(path))
            tmp = path + ".foldersync.tmp"
            with self.sftp.open(tmp, "wb") as out:
                out.set_pipelined(True)
                while True:
                    buf = src_fh.read(CHUNK)
                    if not buf:
                        break
                    out.write(buf)
            try:
                self.sftp.posix_rename(tmp, path)
            except IOError:
                try:
                    self.sftp.remove(path)
                except IOError:
                    pass
                self.sftp.rename(tmp, path)
            self.sftp.utime(path, (mtime, mtime))

    def delete(self, rel):
        with self.lock:
            self._connect()
            self.sftp.remove(self._abs(rel))

    def hash(self, rel):
        with self.lock:
            self._connect()
            q = "'" + self._abs(rel).replace("'", "'\\''") + "'"
            _, stdout, _ = self.client.exec_command("md5sum " + q, timeout=120)
            line = stdout.read().decode(errors="replace").strip()
            if line:
                return line.split()[0]
        # fall back to streaming the file through SFTP
        h = hashlib.md5()
        with self.open_read(rel) as f:
            for buf in iter(lambda: f.read(CHUNK), b""):
                h.update(buf)
        return h.hexdigest()

    def close(self):
        with self.lock:
            if self.client:
                self.client.close()
            self.client = self.sftp = None


_endpoint_cache = {}
_endpoint_lock = threading.Lock()


def endpoint(spec):
    key = json.dumps(spec, sort_keys=True)
    with _endpoint_lock:
        ep = _endpoint_cache.get(key)
        if ep is None:
            ep = SSHFolder(spec) if spec.get("type") == "ssh" else LocalFolder(spec)
            _endpoint_cache[key] = ep
        return ep


# --------------------------------------------------------------------------- compare / sync

def scan(profile):
    folders = profile["folders"]
    exclude = profile.get("exclude", [])
    listings, errors = [], []
    results = [None] * len(folders)

    def work(i, spec):
        try:
            results[i] = endpoint(spec).list(exclude)
        except Exception as e:  # report per folder, keep going
            results[i] = e

    threads = [threading.Thread(target=work, args=(i, f)) for i, f in enumerate(folders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            errors.append("%s: %s" % (folders[i].get("name", i), r))
            listings.append(None)
        else:
            listings.append(r)

    use_hash = profile.get("compare") == "hash"
    rows = []
    all_paths = sorted(set().union(*[l.keys() for l in listings if l is not None]))
    for rel in all_paths:
        entries = [(l.get(rel) if l is not None else None) for l in listings]
        present = [e for e in entries if e]
        newest = max(range(len(entries)),
                     key=lambda i: entries[i]["mtime"] if entries[i] else -1)
        status = "same"
        if any(l is None for l in listings):
            status = "unknown"
        elif len(present) < len(entries):
            status = "missing"
        elif len({e["size"] for e in present}) > 1:
            status = "diff"
        elif max(e["mtime"] for e in present) - min(e["mtime"] for e in present) > MTIME_TOLERANCE:
            status = "diff"
            if use_hash:
                hashes = set()
                for i, e in enumerate(entries):
                    try:
                        e["hash"] = endpoint(folders[i]).hash(rel)
                        hashes.add(e["hash"])
                    except Exception as ex:
                        hashes.add("err:%s" % ex)
                if len(hashes) == 1:
                    status = "same-content"
        rows.append({"path": rel, "entries": entries, "status": status, "newest": newest})
    return {"rows": rows, "errors": errors,
            "folders": [{"name": f.get("name"), "type": f.get("type")} for f in folders]}


def sync(profile, actions):
    """actions: [{path, op: "copy", source: i, targets: [j..]} | {path, op: "delete", targets: [j..]}]"""
    folders = profile["folders"]
    log = []
    for a in actions:
        rel = a["path"]
        try:
            if a["op"] == "copy":
                src = endpoint(folders[a["source"]])
                mtime = a.get("mtime")
                if mtime is None:
                    mtime = time.time()
                for t in a["targets"]:
                    if t == a["source"]:
                        continue
                    with src.open_read(rel) as fh:
                        endpoint(folders[t]).write(rel, fh, mtime)
                    log.append({"ok": True, "msg": "%s: %s -> %s" % (
                        rel, folders[a["source"]]["name"], folders[t]["name"])})
            elif a["op"] == "delete":
                for t in a["targets"]:
                    endpoint(folders[t]).delete(rel)
                    log.append({"ok": True, "msg": "%s: deleted in %s" % (rel, folders[t]["name"])})
        except Exception as e:
            log.append({"ok": False, "msg": "%s: %s" % (rel, e)})
    return log


def read_text(ep, rel, limit=2 * 1024 * 1024):
    with ep.open_read(rel) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError("File too large to diff")
    if b"\0" in data[:8192]:
        raise ValueError("Binary file")
    return data.decode("utf-8", errors="replace").splitlines()


def diff(profile, rel, a, b):
    folders = profile["folders"]
    la = read_text(endpoint(folders[a]), rel)
    lb = read_text(endpoint(folders[b]), rel)
    return "\n".join(difflib.unified_diff(
        la, lb, fromfile="%s/%s" % (folders[a]["name"], rel),
        tofile="%s/%s" % (folders[b]["name"], rel), lineterm=""))


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html")
            if u.path == "/api/config":
                return self._send(200, load_config())
            if u.path == "/api/scan":
                _, prof = get_profile(q.get("profile"))
                return self._send(200, scan(prof))
            if u.path == "/api/diff":
                _, prof = get_profile(q.get("profile"))
                return self._send(200, {"diff": diff(prof, q["path"], int(q["a"]), int(q["b"]))})
            self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._json_body()
            if u.path == "/api/config":
                if not isinstance(body.get("profiles"), dict) or not body["profiles"]:
                    raise ValueError("Config needs at least one profile")
                save_config(body)
                with _endpoint_lock:  # drop cached connections, settings may have changed
                    for ep in _endpoint_cache.values():
                        ep.close()
                    _endpoint_cache.clear()
                return self._send(200, {"ok": True})
            if u.path == "/api/sync":
                _, prof = get_profile(body.get("profile"))
                return self._send(200, {"log": sync(prof, body["actions"])})
            self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})


def main():
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    CONFIG_PATH = os.path.abspath(args.config)
    load_config()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("FolderSync running on http://%s:%d  (config: %s)" % (args.host, args.port, CONFIG_PATH))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
