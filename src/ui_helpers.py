from typing import Any, Protocol


class PeerSnapshotStore(Protocol):
    def update_peer_snapshots(self, clients: list[dict[str, Any]]) -> None: ...


class PeerClientAPI(Protocol):
    def list_clients(self) -> list[dict[str, Any]]: ...


def format_bytes(value: int | float | None) -> str:
    """Format cumulative bytes using decimal units, matching wg-easy."""
    try:
        amount = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        amount = 0.0
    units = ("B", "KB", "MB", "GB", "TB", "PB", "EB")
    unit_index = 0
    while amount >= 1000 and unit_index < len(units) - 1:
        amount /= 1000
        unit_index += 1
    if unit_index == 0:
        return f"{int(amount)} B"
    return f"{amount:.2f} {units[unit_index]}"


def filter_peer_snapshots(
    clients: list[dict[str, Any]], filter_name: str
) -> list[dict[str, Any]]:
    """Filter cached peer snapshots by activity or enabled state."""
    if filter_name == "all":
        return list(clients)
    if filter_name == "active":
        return [client for client in clients if client.get("is_active")]
    if filter_name == "enabled":
        return [client for client in clients if client.get("enabled")]
    if filter_name == "disabled":
        return [client for client in clients if not client.get("enabled")]
    raise ValueError("invalid peer filter")


def fetch_peer_sample(api: PeerClientAPI) -> list[dict[str, Any]] | None:
    """Read a sample; return None on API failure so stored baselines stay intact."""
    try:
        return api.list_clients()
    except Exception:
        return None


def apply_peer_sample(
    store: PeerSnapshotStore, clients: list[dict[str, Any]] | None
) -> bool:
    """Persist a successful sample without changing state on a failed poll."""
    if clients is None:
        return False
    store.update_peer_snapshots(clients)
    return True


async def delete_message_silently(message: Any) -> None:
    """Delete a bot prompt or user cancellation message where Telegram permits."""
    try:
        await message.delete()
    except Exception:
        pass


async def delete_callback_message(query: Any) -> None:
    """Remove an inline cancellation prompt without leaving a reply behind."""
    await delete_message_silently(query.message)
