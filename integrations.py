"""Read-only Asana/Drive clients and owner-only Telegram transport."""
import os
import re
import time
import requests


class RemoteError(RuntimeError):
    pass


def request_json(session, method, url, **kwargs):
    # Do not print requests exceptions: Telegram URLs contain a secret token.
    for attempt in range(4):
        try:
            response = session.request(method, url, timeout=(10, 40), **kwargs)
        except requests.RequestException:
            if method != 'GET' or attempt == 3:
                raise RemoteError('Сервис недоступен: ошибка соединения') from None
            time.sleep(2 ** attempt)
            continue
        if (response.status_code == 429 or response.status_code >= 500) and method == 'GET' and attempt < 3:
            delay = response.headers.get('Retry-After', '2')
            time.sleep(min(float(delay) if delay.isdigit() else 2, 30))
            continue
        if not response.ok:
            raise RemoteError(f'Сервис вернул HTTP {response.status_code}; проверьте доступ и лимиты')
        try:
            return response.json()
        except ValueError:
            raise RemoteError('Сервис вернул некорректный ответ') from None
    raise RemoteError('Исчерпаны попытки подключения')


TASK_FIELDS = 'name,notes,completed,completed_at,modified_at,assignee.name,memberships.project.name,memberships.section.name,custom_fields.name,custom_fields.display_value,permalink_url'


class Asana:
    base = 'https://app.asana.com/api/1.0/'

    def __init__(self):
        self.session = requests.Session()
        self.session.headers['Authorization'] = 'Bearer ' + os.environ['ASANA_TOKEN']

    def get(self, path, **params):
        return request_json(self.session, 'GET', self.base + path, params=params)

    def pages(self, path, **params):
        params['limit'] = 100
        while True:
            data = self.get(path, **params)
            yield from data['data']
            offset = (data.get('next_page') or {}).get('offset')
            if not offset:
                break
            params['offset'] = offset

    def task(self, gid):
        return self.get('tasks/' + gid, opt_fields=TASK_FIELDS)['data']

    def comments(self, gid):
        return [s for s in self.pages(f'tasks/{gid}/stories', opt_fields='text,created_at,resource_subtype,created_by.name')
                if s.get('resource_subtype') == 'comment_added']

    def tasks(self, board_id, since):
        params = {'project': board_id, 'opt_fields': TASK_FIELDS}
        if since:
            params['modified_since'] = since
        return self.pages('tasks', **params)


DRIVE_FIELDS = 'id,name,mimeType,parents,size,trashed,videoMediaMetadata,imageMediaMetadata,shortcutDetails,createdTime,modifiedTime'


class Drive:
    base = 'https://www.googleapis.com/drive/v3/'

    def __init__(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive.readonly'])
        self.session = AuthorizedSession(credentials)
        self.cache = {}

    def get(self, fid):
        if fid not in self.cache:
            self.cache[fid] = request_json(self.session, 'GET', self.base + 'files/' + fid,
                                          params={'fields': DRIVE_FIELDS, 'supportsAllDrives': 'true'})
        return self.cache[fid]

    def children(self, fid):
        if not re.fullmatch(r'[\w-]+', fid):
            raise RemoteError('Некорректный идентификатор папки')
        params = {'q': f"'{fid}' in parents and trashed=false", 'pageSize': 100,
                  'fields': f'nextPageToken,incompleteSearch,files({DRIVE_FIELDS})',
                  'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
        while True:
            data = request_json(self.session, 'GET', self.base + 'files', params=params)
            if data.get('incompleteSearch'):
                raise RemoteError('Drive вернул неполную выборку; комплектность не подтверждена')
            for f in data.get('files', []):
                self.cache[f['id']] = f
                yield f
            if not data.get('nextPageToken'):
                break
            params['pageToken'] = data['nextPageToken']

    def walk(self, roots, max_items=3000):
        pending, seen, files, warnings = list(roots), set(), {}, []
        while pending:
            fid = pending.pop()
            if fid in seen:
                continue
            seen.add(fid)
            if len(seen) > max_items:
                raise RemoteError('Слишком большая папка сдачи; проверка неполная')
            f = self.get(fid)
            if f.get('trashed'):
                warnings.append(f'{f["name"]}: ссылка ведет в корзину')
            elif f['mimeType'] == 'application/vnd.google-apps.folder':
                pending.extend(x['id'] for x in self.children(fid))
            elif f['mimeType'] == 'application/vnd.google-apps.shortcut':
                warnings.append(f'{f["name"]}: ярлык; проверяется фактическое расположение целевого файла')
                pending.append(f['shortcutDetails']['targetId'])
            else:
                files[fid] = f
        return list(files.values()), warnings

    def ancestors(self, file):
        result, seen = [], set()
        parents = file.get('parents', [])
        while parents:
            if len(parents) != 1 or parents[0] in seen or len(seen) > 30:
                raise RemoteError('Не удалось однозначно проверить путь файла')
            seen.add(parents[0])
            parent = self.get(parents[0])
            result.append(parent)
            parents = parent.get('parents', [])
        return list(reversed(result))


class Telegram:
    def __init__(self):
        self.session = requests.Session()
        self.base = 'https://api.telegram.org/bot' + os.environ['TELEGRAM_BOT_TOKEN'] + '/'
        self.chat = int(os.environ['TELEGRAM_CHAT_ID'])
        self.owner = int(os.environ['TELEGRAM_OWNER_ID'])

    def call(self, method, **payload):
        data = request_json(self.session, 'POST', self.base + method, json=payload)
        if not data.get('ok'):
            raise RemoteError('Telegram не принял запрос')
        return data['result']

    def send(self, text, task_id=None):
        # Plain text avoids markup injection from task/file names.
        lines = [line[i:i+3000] for line in text.splitlines() for i in range(0, max(1, len(line)), 3000)]
        chunks, current = [], ''
        for line in lines:
            if len(current) + len(line) > 3300:
                chunks.append(current)
                current = ''
            current += line + '\n'
        if current:
            chunks.append(current)
        for i, chunk in enumerate(chunks):
            payload = {'chat_id': self.chat, 'text': chunk, 'link_preview_options': {'is_disabled': True}}
            if task_id and i == len(chunks) - 1:
                payload['reply_markup'] = {'inline_keyboard': [[{'text': 'Перепроверить', 'callback_data': 'check:' + task_id}]]}
            self.call('sendMessage', **payload)

    def updates(self, offset):
        return self.call('getUpdates', offset=offset, timeout=0, allowed_updates=['message', 'callback_query'])

    def authorized(self, update):
        obj = update.get('callback_query') or update.get('message') or {}
        message = obj.get('message', obj)
        return obj.get('from', {}).get('id') == self.owner and message.get('chat', {}).get('id') == self.chat
