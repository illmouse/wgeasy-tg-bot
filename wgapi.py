import os
import requests


class WGEasyAPI:
    def __init__(self):
        self.base_url = os.environ["WG_EASY_URL"].rstrip("/")
        self.username = os.environ["WG_EASY_LOGIN"]
        self.password = os.environ["WG_EASY_PASSWORD"]
        self.session = requests.Session()
        self._authenticated = False

    def login(self):
        resp = self.session.post(
            f"{self.base_url}/api/session",
            json={"username": self.username, "password": self.password, "remember": False},
        )
        resp.raise_for_status()
        self._authenticated = True

    def _request(self, method, path, **kwargs):
        if not self._authenticated:
            self.login()
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 401:
            self._authenticated = False
            self.login()
            resp = self.session.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    def list_clients(self):
        return self._request("GET", "/api/client").json()

    def create_client(self, name: str):
        return self._request("POST", "/api/client", json={"name": name, "expiresAt": None}).json()

    def delete_client(self, client_id):
        return self._request("DELETE", f"/api/client/{client_id}").json()

    def get_client_config(self, client_id) -> tuple[bytes, str]:
        """Returns (config_bytes, filename)."""
        resp = self._request("GET", f"/api/client/{client_id}/configuration")
        cd = resp.headers.get("Content-Disposition", "")
        filename = f"peer-{client_id}.conf"
        if 'filename="' in cd:
            filename = cd.split('filename="')[1].rstrip('"')
        return resp.content, filename
