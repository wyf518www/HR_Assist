from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import imaplib
import re
import ssl

from .storage import BusinessError


def subject_matches(subject, keywords, mode):
    matches=[keyword.casefold() in subject.casefold() for keyword in keywords]
    return all(matches) if mode=="all" else any(matches)


class Inbox:
    def __init__(self, config, password):
        self.config, self.password=config,password
        self.client=None

    def __enter__(self):
        try:
            self.client=imaplib.IMAP4_SSL(self.config["host"],self.config["port"],ssl_context=ssl.create_default_context(),timeout=45)
            self.client.login(self.config["account"],self.password)
            status,_=self.client.select("INBOX",readonly=True)
            if status!="OK":
                raise imaplib.IMAP4.error("select failed")
            validity=self.client.response("UIDVALIDITY")[1]
            self.uid_validity=(validity[0] or b"unknown").decode()
            return self
        except (OSError,imaplib.IMAP4.error) as exc:
            self.__exit__(None,None,None)
            raise BusinessError("MAIL_CONNECT","无法连接邮箱，请检查 IMAP 授权码、服务器和网络。") from exc

    def __exit__(self,*args):
        if self.client:
            try:
                self.client.logout()
            except (OSError,imaplib.IMAP4.error):
                pass

    def messages(self, start, end, last_uid=None, previous_validity=None):
        # Broad IMAP day search plus exact INTERNALDATE filtering; UID detects late visibility.
        since=(start-timedelta(days=1)).strftime("%d-%b-%Y")
        status,data=self.client.uid("search",None,f'(SINCE "{since}")')
        if status!="OK":
            raise BusinessError("MAIL_SEARCH","邮箱检索失败，成功时间未推进。")
        identifiers=set(data[0].split() if data and data[0] else [])
        late=set()
        if last_uid and previous_validity==self.uid_validity:
            status,extra=self.client.uid("search",None,f"UID {int(last_uid)+1}:*")
            if status!="OK":
                raise BusinessError("MAIL_SEARCH","增量 UID 检索失败。")
            late={value for value in (extra[0].split() if extra and extra[0] else []) if int(value)>int(last_uid)}
            identifiers|=late
        if previous_validity and previous_validity!=self.uid_validity:
            status,all_ids=self.client.uid("search",None,"ALL")
            if status!="OK":
                raise BusinessError("MAIL_SEARCH","邮箱 UID 重建后的检索失败。")
            # Explicitly rescan server metadata after UIDVALIDITY reset; content hashes avoid re-analysis.
            identifiers|=set(all_ids[0].split() if all_ids and all_ids[0] else [])
            late=identifiers.copy()
        for identifier in sorted(identifiers,key=int):
            status,records=self.client.uid("fetch",identifier,"(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID)])")
            if status!="OK":
                raise BusinessError("MAIL_FETCH","读取邮件元信息失败，待下次继续。")
            pair=next((entry for entry in records if isinstance(entry,tuple)),None)
            if not pair:
                continue  # Deleted between search and fetch.
            metadata,headers=pair
            match=re.search(rb'INTERNALDATE "([^"]+)"',metadata)
            if not match:
                raise BusinessError("MAIL_DATE","邮件缺少服务端接收时间，无法确认处理窗口。")
            received=datetime.strptime(match.group(1).decode(),"%d-%b-%Y %H:%M:%S %z").astimezone(timezone.utc)
            if received>=end or (received<start and identifier not in late):
                continue
            header=BytesParser(policy=policy.default).parsebytes(headers)
            if not subject_matches(str(header.get("Subject","")),self.config["keywords"],self.config["match_mode"]):
                yield {"uid":identifier.decode(),"received_at":received.isoformat(),"skipped":True}
                continue
            size=re.search(rb"RFC822.SIZE (\d+)",metadata)
            if not size or int(size.group(1))>35*1024*1024:
                yield {"uid":identifier.decode(),"received_at":received.isoformat(),"message_id":str(header.get("Message-ID","")),"error":"MAIL_SIZE"}
                continue
            status,body=self.client.uid("fetch",identifier,"(BODY.PEEK[])")
            if status!="OK":
                raise BusinessError("MAIL_FETCH","下载邮件失败，成功时间未推进。")
            content=next((part[1] for part in body if isinstance(part,tuple)),None)
            if content is None:
                raise BusinessError("MAIL_FETCH","下载邮件正文为空。")
            message=BytesParser(policy=policy.default).parsebytes(content)
            attachments=[]
            for part in message.iter_attachments():
                filename=part.get_filename() or "未命名附件"
                attachments.append((filename,part.get_payload(decode=True) or b""))
            yield {"uid":identifier.decode(),"received_at":received.isoformat(),"message_id":str(header.get("Message-ID","")),"subject":str(message.get('Subject','')),"attachments":attachments}
