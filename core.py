"""Pure audit rules. No credentials, network, or mutations."""
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs


def dimensions(text):
    return sorted({(int(a), int(b)) for a, b in
                   re.findall(r'(?<!\d)(\d{3,5})\s*[:xх×_]\s*(\d{3,5})(?!\d)', text or '', re.I)})


def fields(task):
    return {f['name']: f.get('display_value') or '' for f in task.get('custom_fields', [])}


def parse_name(name, project=False):
    p = Path(name).stem.split('_')
    if len(p) != (6 if project else 8) or any(not x for x in p):
        raise ValueError('Неверное число тегов или пустой тег')
    author, offer, geo, date, kind, unique = p[:6]
    size = None if project else '_'.join(p[6:])
    return _validate(author, offer, geo, date, kind, unique, size, project, False)


def _validate(author, offer, geo, date, kind, unique, size, project, winline):
    if not re.fullmatch(r'\d{6}', date):
        raise ValueError('Дата должна быть ДДММГГ')
    try:
        datetime.strptime(date, '%d%m%y')
    except ValueError:
        raise ValueError('Несуществующая дата') from None
    if not re.fullmatch(r'[A-Z]{4}', author):
        raise ValueError('Инициалы должны состоять из четырех заглавных латинских букв')
    if not re.fullmatch(r'[A-Z]{2}', geo) or (winline and geo not in {'RU', 'BY', 'KZ'}):
        raise ValueError('Некорректный тег гео')
    allowed = {'collect'} if project else ({'video', 'banner', 'playable'} if winline else {'video', 'static', 'playable'})
    if kind not in allowed:
        raise ValueError('Неверный тип файла: ' + kind)
    if any(re.search(r'\s', x) for x in (offer, unique)):
        raise ValueError('Пробелы внутри тега')
    dims = None
    if size is not None:
        pattern = r'(\d{3,5})x(\d{3,5})' if winline else r'(\d{3,5})_(\d{3,5})'
        m = re.fullmatch(pattern, size)
        if not m:
            raise ValueError('Неверная запись размера')
        dims = tuple(map(int, m.groups()))
    return dict(author=author, offer=offer.upper(), geo=geo, date=date, kind=kind,
                unique=unique, dims=dims, year=str(datetime.strptime(date, '%d%m%y').year))


def parse_winline(name, project=False):
    p = Path(name).stem.split('_')
    if len(p) != 8 or any(not x for x in p):
        raise ValueError('Winline: должно быть ровно 8 тегов')
    offer, date, os_name, kind, unique, size, geo, author = p
    if offer != 'WINLINE' or os_name != 'IOS':
        raise ValueError('Winline: ожидаются WINLINE и IOS')
    return _validate(author, offer, geo, date, kind, unique, size, project, True)


def is_closed(task, board):
    return bool(task.get('completed')) and any(
        m.get('project', {}).get('gid') == board['id'] and
        m.get('section', {}).get('gid') == board['done_section']
        for m in task.get('memberships', []))


def drive_id(url):
    u = urlparse(url)
    if u.hostname != 'drive.google.com':
        return None
    m = re.search(r'/(?:folders|d)/([\w-]+)', u.path)
    candidate = m.group(1) if m else parse_qs(u.query).get('id', [''])[0]
    return candidate if re.fullmatch(r'[\w-]{10,}', candidate) else None


def delivery_links(comments):
    """Only delivery-labeled URLs are final artifacts; never consume reference links."""
    result = {'creative': set(), 'project': set(), 'unclassified': set()}
    for comment in comments:
        mode = 'unclassified'
        for line in comment.get('text', '').splitlines():
            if re.search(r'ссылки на файлы|креативы\s*:', line, re.I):
                mode = 'creative'
            elif re.search(r'проект\s*:', line, re.I):
                mode = 'project'
            for url in re.findall(r'https://drive\.google\.com/[^\s<>]+', line):
                fid = drive_id(url.rstrip('.,);'))
                if fid:
                    result[mode].add(fid)
    return {k: sorted(v) for k, v in result.items()}


def requirements(task, comments, override=None):
    f = fields(task)
    discussion = '\n'.join(c.get('text', '') for c in comments
                           if 'Ссылки на файлы:' not in c.get('text', ''))
    text = task.get('notes', '') + '\n' + discussion
    numbers = {'две': '2', 'два': '2', 'три': '3', 'четыре': '4', 'пять': '5'}
    normalized = re.sub(r'\b(две|два|три|четыре|пять)\b', lambda m: numbers[m[0]], text.lower())
    counts = {int(n) for n in re.findall(r'\b(\d+)\s+(?:верс\w*|видео\b|стил\w*|вариант\w*)', normalized)}
    notes = []
    if len(counts) > 1:
        notes.append('В ТЗ/комментариях разные количества версий; нужна ручная сверка')
    dims = dimensions(f.get('Разрешение'))
    if not dims:
        dims = dimensions(task.get('notes'))
        notes.append('Поле «Разрешение» пустое; размеры из описания требуют подтверждения')
    geos = sorted(set(re.findall(r'\b[A-Z]{2}\b', f.get('ГЕО', ''))))
    if not geos:
        notes.append('В ТЗ не определено гео')
    endcards = dimensions(f.get('Endcard / Эндкарта'))
    if f.get('Endcard / Эндкарта') and not endcards:
        notes.append('Поле эндкарты заполнено, но размер не распознан')
    kind = {'Видео': 'video', 'Статика': 'static', 'Playable': 'playable', 'Плейбл': 'playable'}.get(f.get('Формат'))
    if kind is None:
        notes.append('Не распознан формат креатива')
    out = dict(sizes=dims, geos=geos, versions=max(counts) if counts else 1,
               kind=kind, endcards=endcards, project_required=True,
               semantic_confirmed=False, variant_names=[], notes=notes)
    if override:
        out.update(override)
        out['sizes'] = [tuple(x) for x in out['sizes']]
        out['endcards'] = [tuple(x) for x in out['endcards']]
    if not out['semantic_confirmed']:
        out['notes'] = list(out['notes']) + ['Смысл ТЗ, изменения в обсуждении и исключение для проекта требуют ручной сверки; автоматическая проверка формальная']
    return out


def audit(task, board, comments, creative_files, project_files, paths, config):
    """paths[file id]: actual ancestor folders, from root to direct parent."""
    errors, review = [], []
    f = fields(task)
    req = requirements(task, comments, config.get('task_overrides', {}).get(task['gid']))
    review.extend(req['notes'])
    if not is_closed(task, board):
        review.append('Задача еще не закрыта по обоим условиям')
    winline = f.get('Оффер/клиент', '').lower() == 'winline'
    offer = f.get('Оффер/клиент', '').upper()
    if not offer:
        review.append('Оффер не заполнен; тег OFFER нельзя подтвердить автоматически')
    parsed = []
    parsed_projects = []
    if not creative_files:
        errors.append('В сдаче не найдено ни одного креатива')
    if req['project_required'] and not project_files:
        errors.append('Проект не найден; нужно загрузить или явно подтвердить исключение')
    for project, files in ((False, creative_files), (True, project_files)):
        names = Counter(x['name'] for x in files)
        for name, n in names.items():
            if n > 1:
                errors.append(f'Разные файлы с одинаковым именем ({n}): {name}')
        for file in files:
            name, fid = file['name'], file['id']
            try:
                p = parse_winline(name, project) if winline else parse_name(name, project)
            except ValueError as exc:
                errors.append(f'{name}: {exc}')
                continue
            if file.get('trashed'):
                errors.append(f'{name}: файл в корзине')
            if str(file.get('size', '')) == '0':
                errors.append(f'{name}: пустой файл')
            if offer and p['offer'] != offer:
                errors.append(f'{name}: оффер отличается от ТЗ ({offer})')
            if req['geos'] and p['geo'] not in req['geos']:
                errors.append(f'{name}: гео отличается от ТЗ ({", ".join(req["geos"])})')
            known = config.get('designer_initials', [])
            if known and p['author'] not in known:
                review.append(f'{name}: инициалы отсутствуют в справочнике дизайнеров')
            root = board.get('project_root' if project else 'creative_root')
            path = paths.get(fid, [])
            root_index = next((i for i, folder in enumerate(path) if folder['id'] == root), None)
            if not root:
                review.append('Не настроена корневая папка для ' + ('проектов' if project else 'креативов'))
            elif root_index is None:
                errors.append(f'{name}: файл вне папки своего борда')
            else:
                parts = [x['name'] for x in path[root_index + 1:]]
                if project:
                    valid = len(parts) >= 3 and parts[0] == p['author'] and parts[1].upper() == p['offer']
                else:
                    valid = (len(parts) >= 4 and parts[0].upper() == p['offer'] and
                             p['geo'] in re.findall(r'\b[A-Z]{2}\b', parts[1]) and parts[2] == p['year'])
                if not valid:
                    errors.append(f'{name}: нарушен порядок папок: ' + ' → '.join(parts))
            if project:
                parsed_projects.append(p)
                if path and path[-1]['name'] != Path(name).stem:
                    review.append(f'{name}: название папки проекта отличается от имени проекта')
                if Path(name).suffix.lower() not in {'.zip', '.rar', '.7z', '.aep', '.psd', '.ai', '.fig', '.prproj', '.blend'}:
                    review.append(f'{name}: неизвестный формат проекта')
                continue
            mime = file.get('mimeType', '')
            ext = Path(name).suffix.lower()
            proper = ((p['kind'] == 'video' and mime == 'video/mp4' and ext == '.mp4') or
                      (p['kind'] in {'static', 'banner'} and mime.startswith('image/') and ext in {'.jpg', '.jpeg', '.png', '.webp'}) or
                      (p['kind'] == 'playable' and ext == '.html'))
            if not proper:
                errors.append(f'{name}: тип содержимого/расширение не соответствует тегу формата')
            media = file.get('videoMediaMetadata') or file.get('imageMediaMetadata') or {}
            actual = (media.get('width'), media.get('height'))
            if p['kind'] != 'playable':
                if not all(actual):
                    review.append(f'{name}: Drive не предоставил фактическое разрешение')
                elif actual != p['dims']:
                    errors.append(f'{name}: фактически {actual[0]}×{actual[1]}, название обещает {p["dims"][0]}×{p["dims"][1]}')
            parsed.append((p, file))
    for project in parsed_projects:
        matched = any(all(project[k] == creative[k] for k in ('offer', 'geo', 'date', 'unique')) for creative, _ in parsed)
        if parsed and not matched:
            review.append('Проект ' + project['unique'] + ': нет точного соответствия креативу; возможно, общий проект нескольких версий')
    groups = defaultdict(list)
    for p, file in parsed:
        groups[(p['geo'], 'static' if p['kind'] == 'banner' else p['kind'], p['unique'])].append((p, file))
    for geo in req['geos']:
        variants = {v for g, k, v in groups if g == geo and k == req['kind']}
        if len(variants) != req['versions']:
            errors.append(f'{geo}: найдено {len(variants)} уникальных имен, ожидается версий {req["versions"]}')
        if req['variant_names'] and variants != set(req['variant_names']):
            errors.append(f'{geo}: уникальные имена не соответствуют подтвержденному списку версий')
        for v in variants:
            items = groups[(geo, req['kind'], v)]
            got = Counter(p['dims'] for p, _ in items)
            for size in req['sizes']:
                if got[size] != 1:
                    errors.append(f'{geo}/{v}: для {size[0]}×{size[1]} найдено {got[size]} файлов вместо 1')
            if req['sizes'] and set(got) - set(req['sizes']):
                review.append(f'{geo}/{v}: найдены дополнительные размеры')
            if req['kind'] == 'playable':
                for p, file in items:
                    demos = [other for q, other in parsed if q['kind'] == 'video' and
                             all(q[k] == p[k] for k in ('author', 'offer', 'geo', 'date', 'unique', 'dims'))]
                    if len(demos) != 1:
                        errors.append(f'{file["name"]}: нужна одна парная MP4-демонстрация')
                    elif file.get('parents') != demos[0].get('parents'):
                        errors.append(f'{file["name"]}: HTML и MP4 должны лежать в одной папке')
        for size in req['endcards']:
            end = [(p, x) for p, x in parsed if p['geo'] == geo and p['kind'] in {'static', 'banner'} and p['dims'] == size]
            if not end:
                errors.append(f'{geo}: нет отдельной эндкарты {size[0]}×{size[1]}')
        if req['endcards'] and len(variants) > 1:
            review.append(f'{geo}: вручную подтвердить, какие эндкарты общие, а какие относятся к отдельным версиям')
    if project_files:
        review.append('Содержимое архивов, открытие проекта и наличие всех исходников не проверялись')
    return dict(task_id=task['gid'], name=task['name'], url=task.get('permalink_url', ''),
                errors=sorted(set(errors)), review=sorted(set(review)),
                counts={'creatives': len(creative_files), 'projects': len(project_files)}, requirements=req)
