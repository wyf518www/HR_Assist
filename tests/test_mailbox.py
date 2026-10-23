from datetime import datetime, timezone
from email.message import EmailMessage
from hr_assist.mailbox import Inbox, subject_matches


def test_subject_policy():
    keys=['【BOSS直聘】','应聘']
    assert subject_matches('【BOSS直聘】应聘研发',keys,'all')
    assert not subject_matches('应聘研发',keys,'all')
    assert subject_matches('应聘研发',keys,'any')


def test_exact_window_late_uid_and_peek(pdf):
    message=EmailMessage();message['Subject']='【BOSS直聘】应聘测试';message['Message-ID']='<synthetic-id>'
    message.set_content('Synthetic test only')
    message.add_attachment(pdf,maintype='application',subtype='pdf',filename='【软件工程师_Test】.pdf')
    raw=message.as_bytes()
    class Client:
        calls=[]
        def uid(self,command,*args):
            self.calls.append((command,args))
            if command=='search':
                return 'OK',[b'1 2 3' if 'SINCE' in args[1] else b'3 4']
            identity,fields=args
            if fields=='(BODY.PEEK[])':return 'OK',[(b'body',raw)]
            dates={b'1':'22-Oct-2026 23:00:00 +0000',b'2':'23-Oct-2026 00:30:00 +0000',b'3':'23-Oct-2026 02:00:00 +0000',b'4':'20-Oct-2026 00:00:00 +0000'}
            meta=f'1 (INTERNALDATE "{dates[identity]}" RFC822.SIZE {len(raw)})'.encode()
            return 'OK',[(meta,raw)]
    inbox=Inbox({'keywords':['【BOSS直聘】','应聘'],'match_mode':'all'},'unused')
    inbox.client=Client();inbox.uid_validity='42'
    start=datetime(2026,10,23,tzinfo=timezone.utc);end=datetime(2026,10,23,1,tzinfo=timezone.utc)
    mails=list(inbox.messages(start,end,2,'42'))
    assert [m['uid'] for m in mails]==['2','4']
    assert mails[0]['attachments'][0][1]==pdf
    assert all('BODY.PEEK' in args[1] for command,args in inbox.client.calls if command=='fetch')
