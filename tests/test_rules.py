import copy
import unittest
from core import parse_name, parse_winline, delivery_links, dimensions, is_closed, requirements, audit


BOARD = dict(id='b', done_section='done', creative_root='cr', project_root='pr')


def task():
    return dict(gid='t', name='Example', notes='Сделать 2 версии', completed=True,
                memberships=[{'project': {'gid': 'b'}, 'section': {'gid': 'done'}}],
                custom_fields=[{'name': k, 'display_value': v} for k, v in {
                    'Оффер/клиент': 'Delimobil', 'ГЕО': 'RU', 'Формат': 'Видео',
                    'Разрешение': '1920:1080, 1080:1920', 'Endcard / Эндкарта': '800x800'}.items()])


def files():
    items = []
    for version in ['roulette', 'roulettev2']:
        for w, h in [(1080, 1920), (1920, 1080)]:
            items.append(dict(id=version+str(w), name=f'OLNE_Delimobil_RU_220825_video_{version}_{w}_{h}.mp4',
                              mimeType='video/mp4', size='100', parents=['delivery'],
                              videoMediaMetadata={'width': w, 'height': h}))
    items.append(dict(id='end', name='OLNE_Delimobil_RU_220825_static_end_800_800.jpg', mimeType='image/jpeg',
                      imageMediaMetadata={'width': 800, 'height': 800}, size='10'))
    return items


def paths(items):
    return {f['id']: [{'id': 'cr', 'name': 'NATIVE'}, {'id': 'o', 'name': 'Delimobil'},
                      {'id': 'g', 'name': 'RU'}, {'id': 'y', 'name': '2025'},
                      {'id': 'delivery', 'name': 'delivery'}] for f in items}


class RulesTest(unittest.TestCase):
    def test_versions_are_one_tag(self):
        self.assertEqual(parse_name('OLNE_Delimobil_RU_220825_video_roulettev2_1920_1080.mp4')['unique'], 'roulettev2')
        with self.assertRaises(ValueError):
            parse_name('OLNE_Delimobil_RU_220825_video_roulette_v2_1920_1080.mp4')

    def test_project_six_tags(self):
        self.assertIsNone(parse_name('OLNE_Delimobil_RU_220825_collect_roulette.zip', True)['dims'])
        with self.assertRaises(ValueError):
            parse_name('OLNE_Delimobil_RU_220825_collect_roulette_1920_1080.zip', True)

    def test_winline_eight_tags_and_size_in_project(self):
        name = 'WINLINE_210826_IOS_collect_Slots_1080x1920_BY_POCH.zip'
        self.assertEqual(parse_winline(name, True)['dims'], (1080, 1920))
        with self.assertRaises(ValueError):
            parse_winline(name.replace('Slots', 'Slots_v2'), True)

    def test_invalid_date(self):
        with self.assertRaises(ValueError):
            parse_name('OLNE_Delimobil_RU_310225_video_roulette_1920_1080.mp4')

    def test_dimensions(self):
        self.assertEqual(dimensions('1920:1080, 1080х1920, 800x800, 9:16'), [(800,800),(1080,1920),(1920,1080)])

    def test_closed_both_and_same_board(self):
        t = task()
        self.assertTrue(is_closed(t, BOARD))
        t['completed'] = False
        self.assertFalse(is_closed(t, BOARD))
        t['completed'] = True
        t['memberships'][0]['project']['gid'] = 'other'
        self.assertFalse(is_closed(t, BOARD))

    def test_links_no_reference_and_multiple_folders(self):
        comments = [{'text': 'Реф\nhttps://drive.google.com/drive/folders/reference123'},
                    {'text': 'Ссылки на файлы:\nhttps://drive.google.com/drive/folders/creative1234\nhttps://drive.google.com/drive/folders/creative5678\nПроект:\nhttps://drive.google.com/file/d/project12345/view'}]
        got = delivery_links(comments)
        self.assertEqual(got['creative'], ['creative1234','creative5678'])
        self.assertEqual(got['project'], ['project12345'])
        self.assertEqual(got['unclassified'], ['reference123'])

    def test_version_instruction_not_multiplied_twice(self):
        t = task()
        t['notes'] = 'Сделать 2 видео. Сделать 2 версии.'
        self.assertEqual(requirements(t, [])['versions'], 2)

    def test_versions_in_comments(self):
        t = task(); t['notes'] = 'Сделать креатив'
        self.assertEqual(requirements(t, [{'text':'можно сделать в 2 стилях'}])['versions'], 2)

    def test_endcard_independent(self):
        req = requirements(task(), [])
        self.assertEqual(req['endcards'], [(800,800)])
        self.assertNotIn((800,800), req['sizes'])

    def test_complete_set_and_shared_endcard(self):
        f = files()
        r = audit(task(), BOARD, [], f, [], paths(f), {'task_overrides': {'t': {'project_required':False}}})
        self.assertEqual(r['errors'], [])
        self.assertTrue(any('общие' in x for x in r['review']))

    def test_duplicate_cannot_replace_missing_size(self):
        f = files(); f[1] = dict(f[0], id='duplicate')
        r = audit(task(), BOARD, [], f, [], paths(f), {})
        self.assertTrue(any('одинаковым именем' in x for x in r['errors']))
        self.assertTrue(any('1920×1080 найдено 0' in x for x in r['errors']))

    def test_actual_dimensions(self):
        f = files(); f[0]['videoMediaMetadata']['width'] = 720
        r = audit(task(), BOARD, [], f, [], paths(f), {})
        self.assertTrue(any('фактически 720' in x for x in r['errors']))

    def test_same_folder_name_different_id(self):
        f = files(); p = paths(f)
        p[f[0]['id']][0]['id'] = 'someone_else'
        r = audit(task(), BOARD, [], f, [], p, {})
        self.assertTrue(any('вне папки' in x for x in r['errors']))

    def test_unknown_root_not_pass(self):
        f = files()
        r = audit(task(), dict(BOARD,creative_root=None), [], f, [], paths(f), {})
        self.assertTrue(any('Не настроена' in x for x in r['review']))

    def test_geo_mismatch(self):
        f = files(); f[0]['name'] = f[0]['name'].replace('_RU_', '_BY_')
        r = audit(task(), BOARD, [], f, [], paths(f), {})
        self.assertTrue(any('гео отличается' in x for x in r['errors']))

    def test_no_metadata_is_review(self):
        f = files(); del f[0]['videoMediaMetadata']
        r = audit(task(), BOARD, [], f, [], paths(f), {})
        self.assertTrue(any('не предоставил' in x for x in r['review']))

    def test_ambiguous_count_is_review(self):
        self.assertTrue(any('разные количества' in x for x in requirements(task(), [{'text':'Теперь 3 версии'}])['notes']))

    def test_unique_name_is_not_author(self):
        f = files(); f[0]['name'] = f[0]['name'].replace('OLNE', 'YULA')
        r = audit(task(), BOARD, [], f, [], paths(f), {'task_overrides': {'t': {'project_required': False}}})
        self.assertFalse(any('уникальных имен' in x for x in r['errors']))


if __name__ == '__main__':
    unittest.main()
