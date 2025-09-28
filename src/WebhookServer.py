import asyncio
import json
import logging
import os

import yaml
from aiohttp import web

from E2EEClient import E2EEClient


class WebhookServer:
    def __init__(self):
        self.matrix_client: E2EEClient = None
        self.WEBHOOK_PORT = int(os.environ.get('WEBHOOK_PORT', 8000))
        self.KNOWN_TOKENS = self._parse_known_tokens(
            os.environ['KNOWN_TOKENS'])

    def _parse_known_tokens(self, rooms: str) -> dict:
        known_tokens = {}

        if not rooms:
            logging.critical("KNOWN_TOKENS is empty or not set.")
            return known_tokens

        # engedjük a szóközös ÉS sortöréses elválasztást is
        for raw in rooms.replace('\n', ' ').split(' '):
            pairs = raw.strip()
            if not pairs:
                continue  # üres elem

            parts = [p.strip() for p in pairs.split(',', maxsplit=2)]
            if len(parts) != 3:
                logging.error(f"Malformed KNOWN_TOKENS entry: '{pairs}'. Expected 'token,room,app_name'. Skipping.")
                continue

            token, room, app_name = parts
            if not token or not room or not app_name:
                logging.error(f"Incomplete KNOWN_TOKENS entry: '{pairs}'. Skipping.")
                continue

            known_tokens[token] = {'room': room, 'app_name': app_name}

        if not known_tokens:
            logging.critical("No valid KNOWN_TOKENS parsed. Please check the add-on configuration.")
        return known_tokens
        
    def get_known_rooms(self) -> set:
        known_rooms = set()
        known_rooms.add(os.environ['MATRIX_ADMIN_ROOM'])
        for token in self.KNOWN_TOKENS:
            known_rooms.add(self.KNOWN_TOKENS[token]['room'])
        return known_rooms

    def _format_message(self, msg_format: str, allow_unicode: bool, data) -> str:
        if msg_format == 'json':
            return json.dumps(data, indent=2, ensure_ascii=(not allow_unicode))
        if msg_format == 'yaml':
            return yaml.dump(data, indent=2, allow_unicode=allow_unicode)

    async def _get_index(self, request: web.Request) -> web.Response:
        return web.json_response({'success': True}, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        })

    async def _post_hook(self, request: web.Request) -> web.Response:
        message_format = os.environ['MESSAGE_FORMAT']
        allow_unicode = os.environ['ALLOW_UNICODE'] == 'True'

        token = request.match_info.get('token', '')
        logging.debug(f"Login token: {token}")
        logging.debug(f"Headers: {request.headers}")

        # 🔹 ÚJ: multipart/image kezelés
        if request.content_type and request.content_type.startswith('multipart/'):
            if token not in self.KNOWN_TOKENS:
                return web.json_response({'error': 'Token mismatch'}, status=404)

            form = await request.post()
            file_field = form.get('image') or form.get('file')
            if not file_field:
                return web.json_response({'error': 'No file field'}, status=400)

            try:
                file_bytes = file_field.file.read()
            except Exception:
                file_bytes = await file_field.read()

            filename = getattr(file_field, 'filename', 'upload.bin')
            mimetype = getattr(file_field, 'content_type', 'application/octet-stream')
            caption = form.get('caption') or None

            await self.matrix_client.send_image(
                file_bytes=file_bytes,
                filename=filename,
                mimetype=mimetype,
                room=self.KNOWN_TOKENS[token]['room'],
                sender=self.KNOWN_TOKENS[token]['app_name'],
                caption=caption,
            )
            return web.json_response({'success': True, 'sent_as': 'image', 'filename': filename})

        payload = await request.read()
        data = payload.decode()
        logging.info(f"Received raw data: {data}")

        if token not in self.KNOWN_TOKENS.keys():
            logging.error(f"Login token '{token}' is not recognized as known token.")
            return web.json_response({'error': 'Token mismatch'}, status=404)

        if message_format not in ['raw', 'json', 'yaml']:
            logging.error(f"Message format '{message_format}' not allowed.")
            return web.json_response({'error': 'Gateway configured with unknown message format'}, status=415)

        extracted_from_messages = None
        original_json = None

        if message_format != 'raw':
            data = dict(await request.post())
            try:
                original_json = await request.json()
                data = original_json
            except:
                logging.error('Error decoding data as JSON.')
            finally:
                logging.debug(f"Decoded data: {data}")
            if message_format == 'json' and isinstance(original_json, dict) and 'message' in original_json:
                try:
                    messages_val = original_json.get('message')
                    if isinstance(messages_val, str):
                        extracted_from_messages = messages_val
                    elif isinstance(messages_val, (list, tuple)):
                        # megpróbáljuk a tipikus {"content": "..."} elemeket összefűzni
                        parts = []
                        for item in messages_val:
                            if isinstance(item, dict):
                                c = item.get('content')
                                if isinstance(c, str):
                                    parts.append(c)
                                elif isinstance(c, list):
                                    # ha a content lista, akkor a benne lévő dict-ek "text" mezőit gyűjtjük
                                    for seg in c:
                                        if isinstance(seg, dict) and isinstance(seg.get('text'), str):
                                            parts.append(seg['text'])
                        if parts:
                            extracted_from_messages = "\n".join(p for p in parts if p).strip()
                        else:
                            # ha nincs felismerhető "content", akkor a teljes messages listát küldjük JSON-ként
                            extracted_from_messages = messages_val
                    else:
                        # egyéb típus: JSON-ként továbbítjuk
                        extracted_from_messages = messages_val
                except Exception as e:
                    logging.error(f'Failed to extract "messages" value: {e}')
            data = self._format_message(message_format, allow_unicode, data)
            if extracted_from_messages is not None:
                if isinstance(extracted_from_messages, str):
                    data = extracted_from_messages
                else:
                    # nem string esetén JSON-ként küldjük csak a value-t
                    try:
                        data = json.dumps(extracted_from_messages, ensure_ascii=(not allow_unicode), indent=2)
                    except Exception:
                        data = str(extracted_from_messages)
        logging.debug(f"{message_format.upper()} formatted data: {data}")
        await self.matrix_client.send_message(
            data,
            self.KNOWN_TOKENS[token]['room'],
            self.KNOWN_TOKENS[token]['app_name']
        )

        return web.json_response({'success': True}, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        })

    async def run(self, matrix_client: E2EEClient) -> None:
        self.matrix_client = matrix_client
        app = web.Application()
        app.router.add_get('/', self._get_index)
        app.router.add_post('/post/{token:[a-zA-Z0-9]+}', self._post_hook)

        runner = web.AppRunner(app)
        await runner.setup()

        site = web.TCPSite(runner, host='0.0.0.0', port=self.WEBHOOK_PORT)
        logging.info('The web server is waiting for events.')
        await site.start()
