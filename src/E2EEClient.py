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
                 UnknownToDeviceEvent)
from termcolor import colored
import traceback


class E2EEClient:
    def __init__(self, join_rooms: set):
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
                ssl=bool(os.environ['MATRIX_SSLVERIFY']),
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
        """Handle events sent to device."""
        print(event)
        try:
            client = self.client

            if isinstance(event, UnknownToDeviceEvent):
                if event.source['type'] == 'm.key.verification.request':
                    print('Received verification request, sending response.')
                    content = {
                        "transaction_id": event.source['content']['transaction_id'],
                        "from_device": client.device_id,
                        "methods": event.source['content']['methods'],
                    }
                    message = ToDeviceMessage(
                        "m.key.verification.ready",
                        event.source['sender'],
                        event.source['content']['from_device'],
                        content,
                    )
                    self.verification_from_device = event.source['content']['from_device']
                    await client.to_device(message, event.source['content']['transaction_id'])

                if event.source['type'] == 'm.key.verification.done':
                    print('Received verification done, sending response....')
                    content = {
                        "transaction_id": event.source['content']['transaction_id'],
                    }
                    message = ToDeviceMessage(
                        "m.key.verification.done",
                        event.source['sender'],
                        self.verification_from_device,
                        content,
                    )
                    await client.to_device(message, event.source['content']['transaction_id'])

            elif isinstance(event, KeyVerificationStart):  # first step
                if "emoji" not in event.short_authentication_string:
                    print(
                        "Other device does not support emoji verification "
                        f"{event.short_authentication_string}."
                    )
                    return
                resp = await client.accept_key_verification(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    print(f"accept_key_verification failed with {resp}")

            elif isinstance(event, KeyVerificationCancel):
                print(
                    f"Verification has been cancelled by {event.sender} "
                    f'for reason "{event.reason}".'
                )

            elif isinstance(event, KeyVerificationKey):
                sas = client.key_verifications[event.transaction_id]
                print(f"{sas.get_emoji()}")
                resp = await client.confirm_short_auth_string(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    print(f"confirm_short_auth_string failed with {resp}")

            elif isinstance(event, KeyVerificationMac):
                sas = client.key_verifications[event.transaction_id]
                try:
                    todevice_msg = sas.get_mac()
                except LocalProtocolError as e:
                    print(f"Cancelled or protocol error: {e}")
                else:
                    resp = await client.to_device(todevice_msg)
                    if isinstance(resp, ToDeviceError):
                        print(f"to_device failed with {resp}")
                    print("Emoji verification was successful!")

            else:
                print(f"Received unexpected event type {type(event)}. Event is {event}. Ignored.")
        except BaseException:
            print(traceback.format_exc())

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
            ignore_unverified_devices=True
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
        from nio.responses import UploadError
        try:
            if sync:
                await self.client.sync(timeout=3000, full_state=True)

            msg_prefix = ""
            if os.environ.get('DISPLAY_APP_NAME') == 'True':
                msg_prefix = f"**{sender}** says:  \n"

            room_obj = self.client.rooms.get(room)
            is_encrypted = getattr(room_obj, "encrypted", True)
            size = len(file_bytes)

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
                from io import BytesIO

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

                mxc = upload_resp.content_uri
                content = {
                    "msgtype": "m.image",
                    "body": filename if not caption else caption,
                    "info": {"mimetype": mimetype, "size": size},
                    "file": {
                        "url": mxc,
                        "iv": enc_info["iv"],
                        "hashes": enc_info["hashes"],
                        "key": enc_info["key"],
                        "v": "v2",
                    },
                }
            else:

                from io import BytesIO
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
                    "body": filename if not caption else caption,
                    "info": {"mimetype": mimetype, "size": size},
                    "url": upload_resp.content_uri,
                }

            if os.environ.get('USE_MARKDOWN') == 'True' and (caption or msg_prefix):
                body_text = f"{msg_prefix}{caption or filename}"
                content["format"] = "org.matrix.custom.html"
                try:
                    content["formatted_body"] = markdown(body_text, extensions=['extra'])
                except Exception:
                    content.pop("format", None)

            await self.client.room_send(
                room_id=room,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True
            )
        except Exception as e:
            logging.error(f"Failed to send image: {e}")

    async def run(self) -> None:
        await self.login()
        self.client.add_event_callback(self._message_callback, RoomMessageText)
        self.client.add_response_callback(self._sync_callback, SyncResponse)
        self.client.add_to_device_callback(self.to_device_callback, None)

        if self.client.should_upload_keys:
            await self.client.keys_upload()

        for room in self.join_rooms:
            await self.client.join(room)
        await self.client.joined_rooms()

        logging.info('The Matrix client is waiting for events.')
        await self.client.sync_forever(timeout=300000, full_state=True)
