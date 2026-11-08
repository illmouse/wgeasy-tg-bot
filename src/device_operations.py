"""WireGuard device state changes, independent of Telegram delivery."""

import asyncio
import hashlib
import logging


logger = logging.getLogger(__name__)


class DeviceOperationError(Exception):
    """A device operation failed with a user-facing explanation."""


class PartialDeletionError(Exception):
    def __init__(self, deleted: int, cause: Exception):
        self.deleted = deleted
        super().__init__(str(cause))


async def create_owned_device(wg, db, name: str, owner: str, *, auto_name: bool, reserved_names: set[str]):
    """Create and record a device; remove it if ownership cannot be persisted."""
    result = await asyncio.to_thread(wg.create_client, name)
    client_id = result.get("clientId")
    if not client_id:
        logger.error("Client creation returned no clientId for %s", name)
        raise DeviceOperationError("Device creation could not be confirmed. Contact an administrator.")
    if auto_name:
        hash_name = hashlib.sha256(str(client_id).encode()).hexdigest()[:8]
        if hash_name.lower() not in reserved_names:
            try:
                await asyncio.to_thread(wg.rename_client, client_id, hash_name)
                name = hash_name
            except Exception:
                logger.exception("Failed to rename newly created device %s", client_id)
    try:
        db.add_peer(name.lower(), owner, client_id)
    except Exception as exc:
        logger.exception("Failed to persist peer %s for %s", name, owner)
        try:
            await asyncio.to_thread(wg.delete_client, client_id)
        except Exception:
            logger.exception("Failed to roll back unowned device %s", client_id)
            raise DeviceOperationError(
                "Device was created but could not be recorded or removed. Contact an administrator."
            ) from exc
        raise DeviceOperationError("Device creation failed; the new device was removed. Please retry.") from exc
    return name, client_id


async def set_user_devices_enabled(wg, db, username: str, enable: bool):
    count = 0
    try:
        by_name = {str(c.get("name", "")).lower(): c for c in await asyncio.to_thread(wg.list_clients)}
        for name in db.peers_of(username):
            c = by_name.get(name)
            if c is None:
                continue
            if enable and c.get("enabled") == False:
                await asyncio.to_thread(wg.enable_client, c["id"])
                count += 1
            elif not enable and c.get("enabled"):
                await asyncio.to_thread(wg.disable_client, c["id"])
                count += 1
    except Exception as exc:
        return count, str(exc)
    return count, None


async def delete_user_devices(wg, db, username: str) -> int:
    """Keep the user and every undeleted device record on partial failure."""
    deleted = 0
    try:
        by_name = {str(c.get("name", "")).lower(): c for c in await asyncio.to_thread(wg.list_clients)}
        for name in db.peers_of(username):
            c = by_name.get(name)
            if c is not None:
                await asyncio.to_thread(wg.delete_client, c["id"])
                deleted += 1
            db.remove_peer(name)
    except Exception as exc:
        raise PartialDeletionError(deleted, exc) from exc
    return deleted
