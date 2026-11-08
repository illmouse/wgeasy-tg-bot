import os
import threading
import re
import requests

# wg-easy v15+ authenticates via /api/auth/password; older versions
# (v14 and Amnezia forks) accept the same payload at /api/session.
AUTH_PATHS = ("/api/auth/password", "/api/session")
REQUEST_TIMEOUT = (3.05, 10)


class WGEasyAPI:
    def __init__(self):
        self.base_url = os.environ["WG_EASY_URL"].rstrip("/")
        if not self.base_url.startswith("https://") and not (
            self.base_url.startswith("http://") and os.environ.get("WG_EASY_ALLOW_HTTP") == "1"
        ):
            raise ValueError("WG_EASY_URL must use HTTPS (set WG_EASY_ALLOW_HTTP=1 for trusted internal HTTP)")
        self.username = os.environ["WG_EASY_LOGIN"]
        self.password = os.environ["WG_EASY_PASSWORD"]
        self.session = requests.Session()
        self._authenticated = False
        self._auth_path = None
        self._request_lock = threading.RLock()

    def login(self):
        candidates = [self._auth_path] if self._auth_path else []
        candidates += [p for p in AUTH_PATHS if p not in candidates]
        resp = None
        for path in candidates:
            resp = self.session.post(
                f"{self.base_url}{path}",
                json={"username": self.username, "password": self.password, "remember": False},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            self._auth_path = path
            break
        else:
            resp.raise_for_status()
        if self.base_url.startswith("http://"):
            # Explicitly opted-in internal HTTP cannot carry Secure cookies.
            for cookie in self.session.cookies:
                cookie.secure = False
        self._authenticated = True

    def _request(self, method, path, **kwargs):
        with self._request_lock:
            return self._request_locked(method, path, **kwargs)

    def _request_locked(self, method, path, **kwargs):
        if not self._authenticated:
            self.login()
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 401:
            self._authenticated = False
            self.login()
            resp = self.session.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    def list_clients(self):
        clients = []
        page = 1
        while page <= 50:
            data = self._request("GET", f"/api/client?page={page}&pageSize=100").json()
            if isinstance(data, list):
                return clients + data if page > 1 else data
            batch = data.get("clients") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                raise ValueError(f"Unexpected /api/client response: {type(data).__name__}")
            clients.extend(batch)
            total = data.get("total")
            if not batch and isinstance(total, int) and len(clients) < total:
                raise ValueError("Client listing ended before reaching the reported total")
            if not batch or (isinstance(total, int) and len(clients) >= total) or (
                not isinstance(total, int) and len(batch) < 100
            ):
                break
            page += 1
        else:
            raise ValueError("Client listing exceeded the 50-page limit")
        return clients

    def create_client(self, name: str):
        return self._request("POST", "/api/client", json={"name": name, "expiresAt": None}).json()

    def delete_client(self, client_id):
        return self._request("DELETE", f"/api/client/{client_id}").json()

    def enable_client(self, client_id) -> dict:
        return self._request("POST", f"/api/client/{client_id}/enable").json()

    def disable_client(self, client_id) -> dict:
        return self._request("POST", f"/api/client/{client_id}/disable").json()

    def get_client(self, client_id) -> dict:
        return self._request("GET", f"/api/client/{client_id}").json()

    def rename_client(self, client_id, new_name: str):
        c = self.get_client(client_id)
        skip = {"id", "userId", "interfaceId", "publicKey", "createdAt", "updatedAt", "endpoint"}
        payload = {k: v for k, v in c.items() if k not in skip}
        payload["name"] = new_name
        return self._request("POST", f"/api/client/{client_id}", json=payload).json()

    def get_client_config(self, client_id) -> tuple[bytes, str]:
        """Returns (config_bytes, filename)."""
        resp = self._request("GET", f"/api/client/{client_id}/configuration")
        cd = resp.headers.get("Content-Disposition", "")
        filename = f"peer-{re.sub(r'[^A-Za-z0-9_-]', '_', str(client_id))}.conf"
        if 'filename="' in cd:
            candidate = cd.split('filename="', 1)[1].split('"', 1)[0]
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", candidate) and candidate not in (".", ".."):
                filename = candidate
        return resp.content, filename
