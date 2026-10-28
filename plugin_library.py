from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://hazzaa.space/plugins-api/v1"

CLIENT_KEY = "9ada40bb6f6a67d5df33af95c6fc0ab73fcdaea66f3b0aa36403e9453be23e14"


def library_category(item):

    return 'Scripts' if str(item.get('kind','')).casefold() in {'script','panel'} else 'Plug-ins'


def available_items(items):

    return sorted((item for item in items if item.get('id')!='instant-4k'
                   and re.sub(r'[^a-z0-9]','',item.get('name','').casefold())!='instant4k'),
                  key=lambda item:(item['name'].casefold(),item['id']))


class DownloadCancelled(Exception):
    pass


class LibraryClient:
    def __init__(self, api: str = API, key: str = CLIENT_KEY):
        self.api = api.rstrip("/")
        self.key = key

    def _request(self, path, *, method="GET", payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        return urllib.request.Request(self.api + path, data=data, method=method,
            headers={"X-Hazzaa-Client": self.key, "User-Agent": "HazzaaPlugins/2.0",
                     "Content-Type": "application/json", **(headers or {})})

    def _json(self, request):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("The library response is too large.")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise OSError(f"Library unavailable (HTTP {exc.code}). Try again later.") from exc

    def catalog(self):
        data = self._json(self._request("/catalog"))
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("items"), list):
            raise ValueError("Unsupported library format.")
        items, seen = [], set()
        for entry in data["items"]:
            if not isinstance(entry, dict):
                raise ValueError("Invalid library item.")
            item = {k: entry.get(k) for k in ("id", "name", "filename", "sha256", "bytes", "kind")}
            if (not isinstance(item["id"], str) or not re.fullmatch(r"[a-z0-9-]{1,80}", item["id"])
                or item["id"] in seen
                or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 180
                or not isinstance(item["filename"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+\.(zip|rar)", item["filename"])
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                or type(item["bytes"]) is not int or not 0 < item["bytes"] <= 10 * 1024**3):
                raise ValueError("Invalid library item metadata.")
            seen.add(item["id"])
            items.append(item)
        return available_items(items)

    def download(self, item, directory: Path, cancel: threading.Event, progress):

        final = directory / item["filename"]
        partial = final.with_suffix(final.suffix + ".part")
        for attempt in range(3):
            try:
                if cancel.is_set():
                    raise DownloadCancelled()
                ticket = self._json(self._request("/ticket", method="POST", payload={"id": item["id"]}))
                ticket_value = ticket.get("ticket")
                if not isinstance(ticket_value, str) or len(ticket_value) > 4096:
                    raise ValueError("Invalid download ticket.")
                path = "/download/" + item["id"] + "?ticket=" + urllib.parse.quote(ticket_value, safe="")
                digest, total = hashlib.sha256(), 0
                with urllib.request.urlopen(self._request(path), timeout=30) as response, partial.open("wb") as out:
                    while True:
                        if cancel.is_set():
                            raise DownloadCancelled()
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > item["bytes"]:
                            raise ValueError("Download exceeds the published size.")
                        out.write(chunk)
                        digest.update(chunk)
                        progress(total)
                if total != item["bytes"] or digest.hexdigest() != item["sha256"]:
                    raise ValueError("Download verification failed; the file will not be installed.")
                partial.replace(final)
                return final
            except (OSError, urllib.error.URLError) as exc:
                partial.unlink(missing_ok=True)
                if attempt == 2:
                    raise OSError(f"Could not download {item['name']}: {exc}") from exc
                if cancel.wait(attempt + 1):
                    raise DownloadCancelled()
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        raise OSError("Download failed.")
