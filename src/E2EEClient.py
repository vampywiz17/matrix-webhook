import json
import logging
import os
import sys
from typing import Optional
from markdown import markdown
from nio import (AsyncClient, 
                 AsyncClientConfig, 
                 LoginResponse, 
                 MatrixRoom,
                 RoomMessageText, 
                 SyncResponse, 
                 KeyVerificationCancel, 
                 KeyVerificationKey, 
                 KeyVerificationMac, 
                 ToDeviceMessage, 
                 KeyVerificationStart,
                 ToDeviceError,
                 LocalProtocolError,
                 UnknownToDeviceEvent,
                 KeyVerificationEvent,
                 UnknownEvent)
from termcolor import colored
import traceback


def patch_mindroom_nio_verification_events() -> None:
    """Runtime compatibility patch for mindroom-nio/vodozemac verification events.

    The mindroom-nio fork can decrypt Olm to-device messages, but its Olm
    handler only returns room key/dummy events. Element's session verification
    flow can arrive as encrypted m.key.verification.* to-device payloads. Without
    this patch those decrypted verification payloads are returned as None, so the
    app callback never sees them.
    """
    try:
        from nio.crypto.olm_machine import Olm
        from nio.events.to_device import (
            EncryptedToDeviceEvent,
            KeyVerificationEvent,
            ToDeviceEvent,
        )
    except Exception as exc:
        logging.warning("Could not apply mindroom-nio verification patch: %s", exc)
        return

    if getattr(Olm, "_matrix_webhook_verification_patch", False):
        return

    original_handle_olm_event = Olm._handle_olm_event
    original_handle_to_device_event = Olm.handle_to_device_event

    def patched_handle_olm_event(self, sender, sender_key, payload):
        event_type = payload.get("type")

        if event_type and event_type.startswith("m.key.verification."):
            event_dict = {
                "sender": sender,
                "type": event_type,
                "content": payload.get("content", {}),
            }
            parsed_event = ToDeviceEvent.parse_event(event_dict)
            logging.warning(
                "Decrypted verification to-device event: type=%s parsed=%s raw=%s",
                event_type,
                type(parsed_event).__name__ if parsed_event else None,
                event_dict,
            )
            return parsed_event

        return original_handle_olm_event(self, sender, sender_key, payload)

    def patched_handle_to_device_event(self, event):
        if isinstance(event, EncryptedToDeviceEvent):
            decrypted_event = self.decrypt_event(event)

            if decrypted_event is None:
                logging.warning(
                    "Encrypted to-device event could not be decrypted/handled: raw=%s",
                    getattr(event, "source", None),
                )
                return None

            if isinstance(decrypted_event, KeyVerificationEvent):
                self.handle_key_verification(decrypted_event)

            return decrypted_event

        return original_handle_to_device_event(self, event)

    Olm._handle_olm_event = patched_handle_olm_event
    Olm.handle_to_device_event = patched_handle_to_device_event
    Olm._matrix_webhook_verification_patch = True
    logging.info("Applied mindroom-nio verification compatibility patch.")


def patch_to_device_debug_logging() -> None:
    """Log raw to-device events before nio decrypts/dispatches them.

    This is intentionally limited to encrypted to-device and verification events,
    so normal Matrix traffic should not become too noisy.
    """
    try:
        from nio.client.async_client import AsyncClient as NioAsyncClient
    except Exception as exc:
        logging.warning("Could not apply to-device debug patch: %s", exc)
        return

    if getattr(NioAsyncClient, "_matrix_webhook_to_device_debug_patch", False):
        return

    original_handle_to_device = NioAsyncClient._handle_to_device

    async def patched_handle_to_device(self, response):
        for index, event in enumerate(getattr(response, "to_device_events", [])):
            source = getattr(event, "source", {}) or {}
            event_type = source.get("type")
            if event_type == "m.room.encrypted" or (
                isinstance(event_type, str)
                and event_type.startswith("m.key.verification.")
            ):
                logging.warning(
                    "RAW to-device before crypto: idx=%s class=%s type=%s raw=%s",
                    index,
                    type(event).__name__,
                    event_type,
                    source,
                )

        return await original_handle_to_device(self, response)

    NioAsyncClient._handle_to_device = patched_handle_to_device
    NioAsyncClient._matrix_webhook_to_device_debug_patch = True
    logging.info("Applied Matrix to-device debug logging patch.")


class E2EEClient:
    def __init__(self, join_rooms: set):
        patch_mindroom_nio_verification_events()
        patch_to_device_debug_logging()
        self.STORE_PATH = os.environ['LOGIN_STORE_PATH']
        self.CONFIG_FILE = f"{self.STORE_PATH}/credentials.json"
        self.verification_from_device = ''

        self.join_rooms = join_rooms
        self.client: AsyncClient = None
        self.client_config = AsyncClientConfig(
            max_limit_exceeded=0,
            max_timeouts=0,
            store_sync_tokens=True,
            encryption_enabled=True,
        )

        self.greeting_sent = False

    def _write_details_to_disk(self, resp: LoginResponse, homeserver) -> None:
        with open(self.CONFIG_FILE, "w") as f:
            json.dump(
                {
                    'homeserver': homeserver,
                    'user_id': resp.user_id,
                    'device_id': resp.device_id,
                    'access_token': resp.access_token
                },
                f
            )

    async def _login_first_time(self) -> None:
        homeserver = os.environ['MATRIX_SERVER']
        user_id = os.environ['MATRIX_USERID']
        pw = os.environ['MATRIX_PASSWORD']
        device_name = os.environ['MATRIX_DEVICE']

        if not os.path.exists(self.STORE_PATH):
            os.makedirs(self.STORE_PATH)

        self.client = AsyncClient(
            homeserver,
            user_id,
            store_path=self.STORE_PATH,
            config=self.client_config,
            ssl=(os.environ['MATRIX_SSLVERIFY'] == 'True'),
        )

        resp = await self.client.login(password=pw, device_name=device_name)

        if (isinstance(resp, LoginResponse)):
            self._write_details_to_disk(resp, homeserver)
        else:
            logging.info(
                f"homeserver = \"{homeserver}\"; user = \"{user_id}\"")
            logging.critical(f"Failed to log in: {resp}")
            sys.exit(1)

    async def _login_with_stored_config(self) -> None:
        if self.client:
            return

        with open(self.CONFIG_FILE, "r") as f:
            config = json.load(f)

            self.client = AsyncClient(
                config['homeserver'],
                config['user_id'],
                device_id=config['device_id'],
                store_path=self.STORE_PATH,
                config=self.client_config,
                ssl=(os.environ.get('MATRIX_SSLVERIFY', 'True') == 'True'),
            )

            self.client.restore_login(
                user_id=config['user_id'],
                device_id=config['device_id'],
                access_token=config['access_token']
            )

    async def login(self) -> None:
        if os.path.exists(self.CONFIG_FILE):
            logging.info('Logging in using stored credentials.')
        else:
            logging.info('First time use, did not find credential file.')
            await self._login_first_time()
            logging.info(
                f"Logged in, credentials are stored under '{self.STORE_PATH}'.")

        await self._login_with_stored_config()

    async def _message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        logging.info(colored(
            f"@{room.user_name(event.sender)} in {room.display_name} | {event.body}",
            'green'
        ))

    async def to_device_callback(self, event):  # noqa
        """Handle all to-device events, including verification events."""
        event_source = getattr(event, "source", {}) or {}
        event_type = event_source.get("type")
        logging.info(
            "to_device event received: class=%s type=%s raw=%s",
            type(event).__name__,
            event_type,
            event_source,
        )

        try:
            client = self.client

            if isinstance(event, UnknownToDeviceEvent):
                content = event_source.get("content", {}) or {}
                sender = event_source.get("sender")

                if event_type == "m.key.verification.request":
                    transaction_id = content.get("transaction_id")
                    from_device = content.get("from_device")

                    if not sender or not from_device or not transaction_id:
                        logging.warning(
                            "Invalid verification request, missing sender/from_device/transaction_id: %s",
                            event_source,
                        )
                        return

                    logging.warning(
                        "Received verification request from %s/%s tx=%s; sending ready.",
                        sender,
                        from_device,
                        transaction_id,
                    )
                    ready_content = {
                        "transaction_id": transaction_id,
                        "from_device": client.device_id,
                        "methods": ["m.sas.v1"],
                    }
                    message = ToDeviceMessage(
                        "m.key.verification.ready",
                        sender,
                        from_device,
                        ready_content,
                    )
                    self.verification_from_device = from_device
                    resp = await client.to_device(message, transaction_id)
                    if isinstance(resp, ToDeviceError):
                        logging.warning("sending verification ready failed with %s", resp)
                    return

                if event_type == "m.key.verification.ready":
                    logging.warning("Received verification ready: %s", event_source)
                    return

                if event_type == "m.key.verification.done":
                    transaction_id = content.get("transaction_id")
                    target_device = content.get("from_device") or self.verification_from_device

                    logging.warning("Received verification done: %s", event_source)
                    if sender and target_device and transaction_id:
                        done_content = {"transaction_id": transaction_id}
                        message = ToDeviceMessage(
                            "m.key.verification.done",
                            sender,
                            target_device,
                            done_content,
                        )
                        await client.to_device(message, transaction_id)
                    return

                if isinstance(event_type, str) and event_type.startswith("m.key.verification."):
                    logging.warning("Unhandled verification to-device event: %s", event_source)
                    return

            elif isinstance(event, KeyVerificationStart):
                logging.warning(
                    "Received verification start from %s tx=%s methods=%s",
                    event.sender,
                    event.transaction_id,
                    event.short_authentication_string,
                )

                if "emoji" not in event.short_authentication_string:
                    logging.warning(
                        "Other device does not support emoji verification: %s",
                        event.short_authentication_string,
                    )
                    return

                resp = await client.accept_key_verification(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    logging.warning("accept_key_verification failed with %s", resp)
                    return

                sas = client.key_verifications[event.transaction_id]
                todevice_msg = sas.share_key()
                resp = await client.to_device(todevice_msg)
                if isinstance(resp, ToDeviceError):
                    logging.warning("verification share_key to_device failed with %s", resp)
                    return

            elif isinstance(event, KeyVerificationCancel):
                logging.warning(
                    "Verification has been cancelled by %s for reason %r.",
                    event.sender,
                    event.reason,
                )

            elif isinstance(event, KeyVerificationKey):
                sas = client.key_verifications[event.transaction_id]
                logging.warning("Verification emoji SAS for tx=%s: %s", event.transaction_id, sas.get_emoji())

                # Automatic acceptance is intentional for this bot.
                resp = await client.confirm_short_auth_string(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    logging.warning("confirm_short_auth_string failed with %s", resp)

            elif isinstance(event, KeyVerificationMac):
                sas = client.key_verifications[event.transaction_id]
                try:
                    todevice_msg = sas.get_mac()
                except LocalProtocolError as e:
                    logging.warning("Verification cancelled or protocol error: %s", e)
                else:
                    resp = await client.to_device(todevice_msg)
                    if isinstance(resp, ToDeviceError):
                        logging.warning("verification mac to_device failed with %s", resp)
                        return
                    logging.warning(
                        "Emoji verification was successful. tx=%s verified=%s devices=%s",
                        event.transaction_id,
                        getattr(sas, "verified", None),
                        getattr(sas, "verified_devices", None),
                    )

            else:
                if event_type == "m.room.encrypted":
                    logging.warning(
                        "Received encrypted to-device event that was not decrypted into a verification event: class=%s raw=%s",
                        type(event).__name__,
                        event_source,
                    )
                else:
                    logging.info("Ignoring to-device event class=%s type=%s", type(event).__name__, event_type)
        except BaseException:
            logging.error("Error while handling to-device event:\n%s", traceback.format_exc())


    async def _unknown_room_event_callback(self, room: MatrixRoom, event: UnknownEvent) -> None:
        """Handle unknown room events, including room-based verification requests."""
        try:
            event_type = getattr(event, "type", None) or event.source.get("type")
            logging.info("room unknown event received: room=%s type=%s raw=%s", room.room_id, event_type, event.source)

            if event_type != "m.key.verification.request":
                return

            client = self.client
            content = event.source.get("content", {})
            sender = event.source.get("sender") or getattr(event, "sender", None)
            from_device = content.get("from_device")
            transaction_id = content.get("transaction_id") or getattr(event, "event_id", None)

            if not sender or not from_device:
                logging.warning("Room verification request is missing sender/from_device: %s", event.source)
                return

            logging.info("Received room-based verification request from %s/%s in %s; sending ready.", sender, from_device, room.room_id)

            ready_content = {
                "from_device": client.device_id,
                "methods": ["m.sas.v1"],
                "m.relates_to": {
                    "rel_type": "m.reference",
                    "event_id": event.event_id,
                },
            }
            if transaction_id:
                ready_content["transaction_id"] = transaction_id

            await client.room_send(
                room_id=room.room_id,
                message_type="m.key.verification.ready",
                content=ready_content,
                ignore_unverified_devices=True,
            )

            if transaction_id:
                to_device_content = {
                    "transaction_id": transaction_id,
                    "from_device": client.device_id,
                    "methods": ["m.sas.v1"],
                }
                message = ToDeviceMessage(
                    "m.key.verification.ready",
                    sender,
                    from_device,
                    to_device_content,
                )
                self.verification_from_device = from_device
                await client.to_device(message, transaction_id)

        except BaseException:
            logging.error("Error while handling unknown room event:\n%s", traceback.format_exc())

    async def _sync_callback(self, response: SyncResponse) -> None:
        logging.info(f"We synced, token: {response.next_batch}")

        if not self.greeting_sent:
            self.greeting_sent = True
            greeting = f"Hi, I'm up and runnig from **{os.environ['MATRIX_DEVICE']}**, waiting for webhooks!"
            await self.send_message(greeting, os.environ['MATRIX_ADMIN_ROOM'], 'Webhook server')

    async def send_message(
        self,
        message: str,
        room: str,
        sender: str,
        sync: Optional[bool] = False
    ) -> None:
        if sync:
            await self.client.sync(timeout=3000, full_state=True)

        msg_prefix = ""
        if os.environ['DISPLAY_APP_NAME'] == 'True':
            msg_prefix = f"**{sender}** says:  \n"

        content = {
            'msgtype': 'm.text',
            'body': f"{msg_prefix}{message}",
        }
        if os.environ['USE_MARKDOWN'] == 'True':
            logging.debug('Markdown formatting is turned on.')
            content['format'] = 'org.matrix.custom.html'
            content['formatted_body'] = markdown(
                f"{msg_prefix}{message}", extensions=['extra'])

        await self.client.room_send(
            room_id=room,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=False
        )

    # 🔹 ÚJ: kép küldése titkosított/nem titkosított szobába
    async def send_image(
        self,
        file_bytes: bytes,
        filename: str,
        mimetype: str,
        room: str,
        sender: str,
        caption: Optional[str] = None,
        sync: Optional[bool] = False
    ) -> None:
        import mimetypes
        from io import BytesIO
        from nio.responses import UploadError
        try:
            if sync:
                await self.client.sync(timeout=3000, full_state=True)

            msg_prefix = ""
            if os.environ.get('DISPLAY_APP_NAME') == 'True':
                msg_prefix = f"**{sender}** says:  \n"

            room_obj = self.client.rooms.get(room)
            is_encrypted = bool(room_obj and getattr(room_obj, "encrypted", False))
            size = len(file_bytes)

            if not mimetype:
                guessed, _ = mimetypes.guess_type(filename)
                mimetype = guessed or "application/octet-stream"

            body_name = os.path.basename(filename) if filename else "image"

            # 3) opcionálisan képméret (Element Web szereti, de nem kötelező)
            info = {"mimetype": mimetype, "size": size}
            try:
                from PIL import Image  # type: ignore
                with Image.open(BytesIO(file_bytes)) as im:
                    w, h = im.size
                    info["w"] = int(w)
                    info["h"] = int(h)
            except Exception:
                pass

            if is_encrypted:
                try:
                    from nio.crypto.attachment import encrypt_attachment  # modern path
                except Exception:
                    try:
                        from nio.crypto import attachment as _attachment
                        encrypt_attachment = _attachment.encrypt_attachment  # type: ignore
                    except Exception:
                        try:
                            from nio.crypto import attachments as _attachment_legacy  # type: ignore
                            encrypt_attachment = _attachment_legacy.encrypt_attachment  # type: ignore
                        except Exception as _e:
                            logging.error(
                                "Matrix E2EE attachment encryption nem elérhető a környezetben. "
                                "Ellenőrizd a matrix-nio[e2e] telepítést és a verziót. "
                                f"Import hiba: {_e}"
                            )
                            raise
                encrypted_bytes, enc_info = encrypt_attachment(file_bytes)

                upload_resp = await self.client.upload(
                    BytesIO(encrypted_bytes),
                    content_type="application/octet-stream",
                    filename=filename,
                    filesize=len(encrypted_bytes),
                )

                # --- normalize upload response ---
                if isinstance(upload_resp, tuple):
                    upload_resp = upload_resp[0]

                if isinstance(upload_resp, UploadError):
                    logging.error(f"Image upload failed: {upload_resp}")
                    return

                mxc = upload_resp.content_uri
               
                content = {
                    "msgtype": "m.image",
                    "body": body_name,
                    "info": info,
                    "file": {
                        "url": mxc,
                        "iv": enc_info["iv"],
                        "hashes": enc_info["hashes"],
                        "key": enc_info["key"],
                        "v": "v2",
                    },
                }
            else:
                upload_resp = await self.client.upload(
                    BytesIO(file_bytes),
                    content_type=mimetype,
                    filename=filename,
                    filesize=len(file_bytes),
                )

                # --- normalize upload response ---
                if isinstance(upload_resp, tuple):
                    upload_resp = upload_resp[0]

                if isinstance(upload_resp, UploadError):
                    logging.error(f"Image upload failed: {upload_resp}")
                    return
                content = {
                    "msgtype": "m.image",
                    "body": body_name,
                    "info": info,
                    "url": upload_resp.content_uri,
                }

            if os.environ.get('USE_MARKDOWN') == 'True' and (caption or msg_prefix):
                body_text = f"{msg_prefix}{caption or body_name}"
                content["format"] = "org.matrix.custom.html"
                try:
                    content["formatted_body"] = markdown(body_text, extensions=['extra'])
                except Exception:
                    content.pop("format", None)

            await self.client.room_send(
                room_id=room,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=False
            )
        except Exception as e:
            logging.error(f"Failed to send image: {e}")
            raise

    async def run(self) -> None:
        await self.login()
        logging.info("Current Matrix user/device: %s / %s", self.client.user_id, self.client.device_id)
        self.client.add_event_callback(self._message_callback, RoomMessageText)
        self.client.add_event_callback(self._unknown_room_event_callback, UnknownEvent)
        self.client.add_response_callback(self._sync_callback, SyncResponse)
        # filter=None is intentional: we also want to see encrypted to-device events
        # that mindroom-nio could not decrypt/parse into a higher level event.
        self.client.add_to_device_callback(self.to_device_callback, None)

        if self.client.should_upload_keys:
            await self.client.keys_upload()

        for room in self.join_rooms:
            await self.client.join(room)
        await self.client.joined_rooms()

        logging.info('The Matrix client is waiting for events.')
        await self.client.sync(timeout=30000, full_state=True)
        await self.client.sync_forever(timeout=55000, full_state=False)
