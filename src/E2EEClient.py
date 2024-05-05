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
                    'homeserver': homeserver,  # e.g. "https://matrix.example.org"
                    'user_id': resp.user_id,  # e.g. "@user:example.org"
                    'device_id': resp.device_id,  # device ID, 10 uppercase letters
                    'access_token': resp.access_token  # cryptogr. access token
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
                    print('Received verification request, sending response....')
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

                sas = client.key_verifications[event.transaction_id]

                todevice_msg = sas.share_key()
                resp = await client.to_device(todevice_msg)
                if isinstance(resp, ToDeviceError):
                    print(f"to_device failed with {resp}")

            elif isinstance(event, KeyVerificationCancel):  # anytime
                # There is no need to issue a
                # client.cancel_key_verification(tx_id, reject=False)
                # here. The SAS flow is already cancelled.
                # We only need to inform the user.
                print(
                    f"Verification has been cancelled by {event.sender} "
                    f'for reason "{event.reason}".'
                )

            elif isinstance(event, KeyVerificationKey):  # second step
                
                sas = client.key_verifications[event.transaction_id]

                print(f"{sas.get_emoji()}")

                resp = await client.confirm_short_auth_string(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    print(f"confirm_short_auth_string failed with {resp}")

            elif isinstance(event, KeyVerificationMac):  # third step
                
                sas = client.key_verifications[event.transaction_id]
                try:
                    todevice_msg = sas.get_mac()
                except LocalProtocolError as e:
                    # e.g. it might have been cancelled by ourselves
                    print(
                        f"Cancelled or protocol error: Reason: {e}.\n"
                        f"Verification with {event.sender} not concluded. "
                        "Try again?"
                    )
                else:
                    resp = await client.to_device(todevice_msg)
                    if isinstance(resp, ToDeviceError):
                        print(f"to_device failed with {resp}")
                    print(
                        f"sas.we_started_it = {sas.we_started_it}\n"
                        f"sas.sas_accepted = {sas.sas_accepted}\n"
                        f"sas.canceled = {sas.canceled}\n"
                        f"sas.timed_out = {sas.timed_out}\n"
                        f"sas.verified = {sas.verified}\n"
                        f"sas.verified_devices = {sas.verified_devices}\n"
                    )
                    print(
                        "Emoji verification was successful!\n"
                        "Hit Control-C to stop the program or "
                        "initiate another Emoji verification from "
                        "another device or room."
                    )
            else:
                print(
                    f"Received unexpected event type {type(event)}. "
                    f"Event is {event}. Event will be ignored."
                )
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
            # Markdown formatting removes YAML newlines if not padded with spaces,
            # and can also mess up posted data like system logs
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
