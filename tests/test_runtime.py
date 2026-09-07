import tempfile
import time
import unittest
from unittest.mock import patch
from app import Bot, State, iso
from integrations import Drive, Telegram, Asana


BOARD = {'id':'b','done_section':'done','name':'Test'}


class FakeAsana:
    def __init__(self, tasks):
        self.rows = tasks
    def tasks(self, board, since):
        return self.rows
    def task(self, gid):
        return next(t for t in self.rows if t['gid'] == gid)


def task(gid='t', completed_at=None):
    return {'gid':gid,'name':gid,'completed':True,'completed_at':iso(completed_at or time.time()),
            'memberships':[{'project':{'gid':'b'},'section':{'gid':'done'}}]}


class RuntimeTest(unittest.TestCase):
    def test_persist_start_and_old_tasks_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            path = d+'/s.db'
            state = State(path)
            self.assertIsNone(state.get('started_at'))
            start = time.time()-100
            state.set('started_at', start)
            bot = Bot.__new__(Bot)
            bot.state = State(path)
            bot.config = {'boards':[BOARD], 'grace_seconds':20, 'recheck_seconds':0, 'recheck_days':7}
            bot.asana = FakeAsana([task('old',start-1),task('new',start+1)])
            sent = []
            bot.tg = type('TG',(),{'send':lambda self,*args:sent.append(args)})()
            bot.check = lambda t,b: {'name':t['name'],'url':'','errors':['Missing'],'review':[], 'counts':{'creatives':0,'projects':0}}
            bot.asana.rows[1]['completed'] = False
            bot.poll()
            bot.asana.rows[1]['completed'] = True
            bot.poll()
            self.assertIsNone(state.job('b:old'))
            self.assertEqual(sent, [])  # first observation must wait grace period
            state.db.execute('UPDATE jobs SET eligible_at=?', (time.time()-30,)); state.db.commit()
            bot.poll(); self.assertEqual(len(sent),1)
            bot.poll(); self.assertEqual(len(sent),1)  # identical report not repeated
            bot.asana.rows[1]['completed'] = False
            bot.poll(); self.assertIsNone(state.job('b:new'))
            self.assertEqual(float(bot.state.get('started_at')),start)

    def test_recursive_drive_dedup_and_cycle(self):
        drive = Drive.__new__(Drive)
        objs = {'root':{'id':'root','name':'root','mimeType':'application/vnd.google-apps.folder'},
                'child':{'id':'child','name':'child','mimeType':'application/vnd.google-apps.folder'},
                'f':{'id':'f','name':'file.mp4','mimeType':'video/mp4'}}
        drive.get = lambda fid: objs[fid]
        drive.children = lambda fid: [objs['child'],objs['f']] if fid=='root' else [objs['f'],objs['root']]
        files,warnings = drive.walk(['root','child','f'])
        self.assertEqual([f['id'] for f in files],['f'])

    def test_asana_pagination(self):
        a = Asana.__new__(Asana)
        a.get = lambda path,**p: {'data':[2],'next_page':None} if p.get('offset') else {'data':[1],'next_page':{'offset':'next'}}
        self.assertEqual(list(a.pages('tasks')),[1,2])

    def test_drive_pagination(self):
        drive = Drive.__new__(Drive); drive.session=None; drive.cache={}
        with patch('integrations.request_json', side_effect=[{'files':[{'id':'a'}],'nextPageToken':'token'}, {'files':[{'id':'b'}]}]) as req:
            self.assertEqual([f['id'] for f in drive.children('folder')],['a','b'])
            self.assertEqual(req.call_count,2)

    def test_telegram_owner_and_chat(self):
        t = Telegram.__new__(Telegram); t.owner=5; t.chat=6
        self.assertTrue(t.authorized({'message':{'from':{'id':5},'chat':{'id':6}}}))
        self.assertFalse(t.authorized({'message':{'from':{'id':7},'chat':{'id':6}}}))
        self.assertFalse(t.authorized({'callback_query':{'from':{'id':5},'message':{'chat':{'id':7}}}}))

    def test_telegram_long_reports_preserve_text(self):
        t = Telegram.__new__(Telegram); t.chat=6
        calls=[]; t.call=lambda method,**payload:calls.append(payload)
        text='a'*8000+'\nконец'
        t.send(text,'123')
        self.assertTrue(all(len(x['text'])<=3301 for x in calls))
        self.assertEqual(''.join(x['text'] for x in calls).replace('\n',''),text.replace('\n',''))
        self.assertIn('reply_markup',calls[-1])


if __name__ == '__main__': unittest.main()
