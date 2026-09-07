import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from core import audit, delivery_links, fields, is_closed
from integrations import Asana, Drive, Telegram, RemoteError


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else 0


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,eligible_at REAL,checked_at REAL,signature TEXT,report TEXT)')

    def get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))

    def job(self, key):
        return self.db.execute('SELECT eligible_at,checked_at,signature FROM jobs WHERE key=?', (key,)).fetchone()

    def observe(self, key, now, eligible):
        with self.db:
            if not eligible:
                self.db.execute('DELETE FROM jobs WHERE key=?', (key,))
            else:
                self.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,0,?,?)', (key, now, '', ''))

    def save(self, key, report, signature, now):
        with self.db:
            self.db.execute('UPDATE jobs SET checked_at=?,signature=?,report=? WHERE key=?',
                            (now, signature, json.dumps(report, ensure_ascii=False), key))


def report_text(report):
    title = '❌ Найдены ошибки' if report['errors'] else '⚠️ Нужна ручная сверка' if report['review'] else '✅ Проверка пройдена'
    lines = [title, report['name'], report['url'],
             f'Креативов: {report["counts"]["creatives"]}; файлов проекта: {report["counts"]["projects"]}']
    if report['errors']:
        lines += ['', 'Исправить:'] + ['• ' + x for x in report['errors']]
    if report['review']:
        lines += ['', 'Проверить вручную:'] + ['• ' + x for x in report['review']]
    return '\n'.join(lines)


class Bot:
    def __init__(self, config, state, telegram=True):
        self.config, self.state = config, state
        self.asana, self.drive = Asana(), Drive()
        self.tg = Telegram() if telegram else None

    def resolve_roots(self, board):
        """Resolve only inside the confirmed personal root; no global title guessing."""
        board = dict(board)
        if board.get('personal_root') and (not board.get('creative_root') or not board.get('project_root')):
            children = list(self.drive.children(board['personal_root']))
            for key, name in [('creative_root', 'buying web summon'), ('project_root', 'ae_projects')]:
                if board.get(key):
                    continue
                candidates = [x for x in children if x['mimeType'] == 'application/vnd.google-apps.folder' and x['name'].lower() == name]
                if len(candidates) == 1:
                    board[key] = candidates[0]['id']
        return board

    def check(self, task, board):
        self.drive.cache.clear()
        board = self.resolve_roots(board)
        comments = self.asana.comments(task['gid'])
        links = delivery_links(comments)
        creative, cw = self.drive.walk(links['creative'])
        project, pw = self.drive.walk(links['project'])
        paths = {file['id']: self.drive.ancestors(file) for file in creative + project}
        result = audit(task, board, comments, creative, project, paths, self.config)
        result['review'].extend(cw + pw)
        if links['unclassified']:
            result['review'].append('Есть ссылки Drive без подписи «Ссылки на файлы:»/«Проект:»; вручную определить финальную сдачу')
        if not links['creative']:
            result['errors'].append('В комментариях нет размеченных ссылок на креативы')
        if board.get('department') and fields(task).get('Департамент') != board['department']:
            result['review'].append('Для этого департамента на борде RockApp маршрут еще не подтвержден; проверка папки NATIVE неприменима')
            result['errors'] = [x for x in result['errors'] if 'вне папки своего борда' not in x]
        result['errors'] = sorted(set(result['errors']))
        result['review'] = sorted(set(result['review']))
        return result

    def poll(self):
        now = time.time()
        start = float(self.state.get('started_at'))
        for board in self.config['boards']:
            try:
                baseline = self.state.get('baseline:' + board['id'])
                if baseline is None:
                    before = time.time()
                    existing = list(self.asana.tasks(board['id'], None))
                    for t in existing:
                        if is_closed(t, board):
                            self.state.set('old:' + board['id'] + ':' + t['gid'], 1)
                    self.state.set('baseline:' + board['id'], before)
                    continue
                for task in self.asana.tasks(board['id'], iso(float(baseline))):
                    key = board['id'] + ':' + task['gid']
                    eligible = is_closed(task, board)
                    if self.state.get('old:' + key) == '1':
                        if eligible:
                            continue
                        self.state.set('old:' + key, 0)
                    self.state.observe(key, now, eligible)
                    if not eligible:
                        continue
                    first, checked, old_signature = self.state.job(key)
                    if now - first < self.config['grace_seconds']:
                        continue
                    if checked and (now - checked < self.config['recheck_seconds'] or now - first > self.config['recheck_days'] * 86400):
                        continue
                    try:
                        fresh = self.asana.task(task['gid'])
                        if not is_closed(fresh, board):
                            self.state.observe(key, now, False)
                            continue
                        result = self.check(fresh, board)
                        signature = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                        if signature != old_signature and (result['errors'] or result['review'] or old_signature):
                            self.tg.send(report_text(result), task['gid'])
                        self.state.save(key, result, signature, time.time())
                    except RemoteError as exc:
                        self.alert_once('task:' + key, f'Не удалось проверить задачу {task["gid"]}: {exc}')
            except RemoteError as exc:
                self.alert_once('board:' + board['id'], f'Не удалось прочитать борд {board["name"]}: {exc}')
        self.state.set('last_poll', iso(time.time()))

    def alert_once(self, key, text):
        last = float(self.state.get('alert:' + key) or 0)
        if time.time() - last > 3600:
            self.tg.send(text)
            self.state.set('alert:' + key, time.time())

    def manual(self, gid, send=True):
        task = self.asana.task(gid)
        boards = [b for b in self.config['boards'] if any(m['project']['gid'] == b['id'] for m in task.get('memberships', []))]
        if not boards:
            raise RemoteError('Задача не относится к настроенным дизайн-бордам')
        reports = []
        for b in boards:
            result = self.check(task, b)
            reports.append(result)
            if send:
                self.tg.send(report_text(result), gid)
        return reports

    def commands(self):
        for update in self.tg.updates(int(self.state.get('telegram_offset') or 0)):
            if self.tg.authorized(update):
                obj = update.get('callback_query')
                text = obj.get('data', '') if obj else update['message'].get('text', '')
                if obj:
                    self.tg.call('answerCallbackQuery', callback_query_id=obj['id'])
                try:
                    if text.startswith('/status'):
                        self.tg.send('Бот работает.\nНачало контроля: ' + iso(float(self.state.get('started_at'))) +
                                     '\nПоследний обход: ' + (self.state.get('last_poll') or 'еще не выполнен'))
                    elif text.startswith(('/check ', 'check:')):
                        m = re.search(r'/task/(\d+)', text) or re.search(r'(\d{13,})', text)
                        if not m:
                            self.tg.send('Пришли /check и ссылку на задачу Asana.')
                        else:
                            self.manual(m[1])
                    else:
                        self.tg.send('Команды:\n/status — состояние\n/check ССЫЛКА — проверить задачу, включая старую\nПосле исправлений можно нажать «Перепроверить».')
                except RemoteError as exc:
                    self.tg.send('Проверка не завершена: ' + str(exc))
            self.state.set('telegram_offset', update['update_id'] + 1)


def main():
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'check', 'doctor'])
    parser.add_argument('task_id', nargs='?')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    config = json.loads(Path(os.environ.get('BOT_CONFIG', 'config.json')).read_text())
    state = State(os.environ.get('BOT_DB', 'data/state.sqlite3'))
    bot = Bot(config, state, telegram=args.command != 'check')
    if args.command == 'check':
        if not args.task_id or not args.task_id.isdigit():
            parser.error('Нужен числовой ID задачи')
        print(json.dumps(bot.manual(args.task_id, send=False), ensure_ascii=False, indent=2))
    elif args.command == 'doctor':
        print('Asana:', bot.asana.get('users/me', opt_fields='gid')['data']['gid'])
        for b in config['boards']:
            sections = list(bot.asana.pages(f'projects/{b["id"]}/sections', opt_fields='name'))
            assert any(s['gid'] == b['done_section'] for s in sections), 'Не найден раздел Готово'
            resolved = bot.resolve_roots(b)
            for key in ('creative_root', 'project_root'):
                print(b['name'], key, bot.drive.get(resolved[key])['name'] if resolved.get(key) else 'НЕ НАСТРОЕНО')
        print('Telegram bot:', bot.tg.call('getMe')['username'])
        print('Проверка соединений закончена. Сообщения не отправлялись.')
    else:
        # Persist the initial start marker so restarts never replay old completions.
        if state.get('started_at') is None:
            state.set('started_at', time.time())
        next_poll = 0
        while True:
            try:
                bot.commands()
                if time.time() >= next_poll:
                    bot.poll()
                    next_poll = time.time() + config['poll_seconds']
            except RemoteError:
                logging.warning('Временная ошибка сервиса; повтор в следующем цикле')
            time.sleep(2)


if __name__ == '__main__':
    try:
        main()
    except (RemoteError, KeyError) as exc:
        # Do not include secrets or upstream request objects in tracebacks.
        raise SystemExit('Не удалось запустить: ' + str(exc)) from None
