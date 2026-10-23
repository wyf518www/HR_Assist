from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import shutil
import threading
import time

from .assessment import CloudModel, FIELD_LABELS, assess, assessment_key, default_criteria
from .config import DEFAULT_SETTINGS
from .db import Database, dump, now, uid
from .documents import extract_file
from .mailbox import Inbox
from .instance import InstanceLock
from .reports import applications, empty_cells, overview_export, role_reports
from .security import Vault, password_hash
from .storage import BusinessError, Storage, classify_filename, safe_filename


class Service:
    def __init__(self, environment):
        self.env=environment
        self.instance_lock=InstanceLock(environment.state_dir)
        self.db=Database(environment.state_dir)
        self.storage=Storage(environment.data_dir,self.db)
        self.vault=Vault(environment.state_dir)
        if not self.db.get("admin"):
            if len(environment.admin_password)<12 or environment.admin_password.startswith("CHANGE_ME"):
                raise RuntimeError("首次启动需设置至少12位的 HR_ADMIN_PASSWORD；不要使用示例占位值。")
            self.db.put("admin",{"username":environment.admin_username,"hash":password_hash(environment.admin_password)})
        if not self.db.get("settings"):
            self.db.put("settings",deepcopy(DEFAULT_SETTINGS))
        self.run_lock=threading.Lock()
        self.ingest_lock=threading.Lock()
        self.stop_event=threading.Event()
        self.worker=None
        self.scheduler_thread=None

    def settings(self):
        return self.db.get("settings")

    def public_settings(self):
        config=self.settings()
        config["mail"]["password_configured"]=bool(self.db.get("mail_secret"))
        config["llm"]["api_key_configured"]=bool(self.db.get("model_secret"))
        return config

    def save_settings(self,data):
        config=self.settings()
        config["mail"].update(account=data.mail_account,host=data.mail_host,port=data.mail_port,
                              match_mode=data.subject_mode,enabled=data.mail_enabled)
        config["llm"].update(endpoint=data.llm_endpoint,model=data.llm_model,approved=data.llm_approved,
                             approval_note=data.approval_note,max_calls_per_run=data.max_calls_per_run)
        config["schedule"].update(enabled=data.schedule_enabled,time=data.schedule_time)
        config["policy"].update(confirmed=data.policy_confirmed,master_bypass=data.master_bypass,
                                schools=list(dict.fromkeys(x.strip() for x in data.schools)),school_source=data.school_source)
        with self.db.connect(True) as con:
            for key,value in (("settings",config),("mail_secret",self.vault.encrypt(data.mail_password)) if data.mail_password is not None else (None,None),
                              ("model_secret",self.vault.encrypt(data.llm_api_key)) if data.llm_api_key is not None else (None,None)):
                if key:
                    con.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,dump(value)))
        self.db.event("SETTINGS_UPDATED","配置已修改，凭据未写入日志。")
        return self.public_settings()

    def model(self):
        return CloudModel(self.settings()["llm"],self.vault.decrypt(self.db.get("model_secret","")))

    def roles(self):
        rows=self.db.all("SELECT r.*,j.text jd_text,j.fields,j.criteria,j.approved,j.missing,j.source_file_id FROM roles r LEFT JOIN jd_versions j ON r.current_version=j.id ORDER BY r.name")
        for row in rows:
            for key in ("fields","criteria","missing"):
                row[key]=json.loads(row[key]) if row[key] else ({} if key=="fields" else [])
        return rows

    def save_role(self,data,identity=None):
        fields=data.fields.model_dump()
        missing=[FIELD_LABELS[k] for k,v in fields.items() if not v.strip()]
        if not data.jd_text.strip():
            missing.append("JD 原文")
        if data.approved and missing:
            raise BusinessError("JD_INCOMPLETE","JD 缺失项："+"、".join(missing))
        if data.source_file_id:
            file,_=self.storage.file(data.source_file_id)
            if file["kind"]!="jd":
                raise BusinessError("JD_FILE","只能关联已上传的 JD 文件。")
        identity=identity or uid()
        version=uid()
        criteria=[c.model_dump() for c in data.criteria] or default_criteria(fields)
        if data.criteria:
            old=self.db.one("SELECT j.fields FROM roles r JOIN jd_versions j ON j.id=r.current_version WHERE r.id=?",(identity,))
            if old:
                before={c['id']:c for c in default_criteria(json.loads(old['fields']))}
                after={c['id']:c for c in default_criteria(fields)}
                for item in criteria:
                    if item['id'] in before and item['description']==before[item['id']]['description']:
                        item['description']=after[item['id']]['description']
        try:
            with self.db.connect(True) as con:
                exists=con.execute("SELECT id FROM roles WHERE id=?",(identity,)).fetchone()
                if exists:
                    con.execute("UPDATE roles SET name=?,is_sales=? WHERE id=?",(data.name,data.is_sales,identity))
                else:
                    con.execute("INSERT INTO roles VALUES (?,?,?,?,?)",(identity,data.name,data.is_sales,None,now()))
                con.execute("INSERT INTO jd_versions VALUES (?,?,?,?,?,?,?,?,?)",(version,identity,data.jd_text,dump(fields),dump(criteria),data.approved,dump(missing),data.source_file_id,now()))
                con.execute("UPDATE roles SET current_version=? WHERE id=?",(version,identity))
        except __import__("sqlite3").IntegrityError as exc:
            raise BusinessError("ROLE_DUPLICATE","岗位名称已存在。") from exc
        self.db.event("JD_SAVED","已保存新的 JD 版本。")
        return next(r for r in self.roles() if r["id"]==identity)

    def upload_jd(self,data,name):
        if Path(name).suffix.lower() not in {".pdf",".docx",".txt"}:
            raise BusinessError("JD_FORMAT","JD 支持 PDF、DOCX、UTF-8 TXT。")
        file=self.storage.put(data,name,"jd")
        _,path=self.storage.file(file["id"])
        spans=extract_file(path)
        return {"file_id":file["id"],"text":"\n\n".join(s["text"] for s in spans),"spans":spans}

    def ingest(self,data,name,role_id=None,mail_event_id=None):
        with self.ingest_lock:
            return self._ingest(data,name,role_id,mail_event_id)

    def _ingest(self,data,name,role_id=None,mail_event_id=None):
        if Path(name).suffix.lower()!=".pdf":
            raise BusinessError("RESUME_FORMAT","简历只接受 PDF 文件。")
        if len(data)>self.env.max_upload_bytes:
            raise BusinessError("UPLOAD_LIMIT","单个附件不能超过20MB。")
        if not data[:1024].lstrip().startswith(b"%PDF-"):
            raise BusinessError("INVALID_PDF","附件后缀为PDF，但文件内容不是PDF。")
        role_name,candidate_name=classify_filename(name)
        if role_id:
            if not self.db.one("SELECT id FROM roles WHERE id=?",(role_id,)):
                raise BusinessError("ROLE_NOT_FOUND","岗位不存在。",404)
        elif role_name:
            role=self.db.one("SELECT id FROM roles WHERE name=?",(role_name,))
            role_id=role["id"] if role else None
        file=self.storage.put(data,name,"resumes",folder="temporary",deduplicate=True)
        existing=self.db.one("SELECT id FROM applications WHERE file_id=? AND ifnull(role_id,'')=?",(file["id"],role_id or ""))
        identity=existing["id"] if existing else uid()
        with self.db.connect(True) as con:
            if not existing:
                con.execute("INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?)",(identity,file["id"],role_id,candidate_name or "待确认",name,"QUEUED",None,None,now()))
                con.execute("INSERT INTO followups VALUES (?,?,0,?)",(identity,dump(empty_cells()),now()))
            if mail_event_id:
                con.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?,?)",(uid(),mail_event_id,file["sha256"],file["id"],identity,"DUPLICATE" if existing else "REGISTERED"))
        self.db.event("DUPLICATE" if existing else "RESUME_REGISTERED","重复原件已关联既有申请。" if existing else "PDF 原件已登记。",application_id=identity)
        return {"id":identity,"duplicate":bool(existing),"file_id":file["id"]}

    def get_application(self,identity):
        row=next((r for r in applications(self.db) if r["id"]==identity),None)
        if not row:
            raise BusinessError("APPLICATION_NOT_FOUND","申请不存在。",404)
        document=self.db.one("SELECT * FROM documents WHERE file_id=?",(row["file_id"],))
        row["document"]={**document,"spans":json.loads(document["spans"])} if document else None
        row["history"]=self.db.all("SELECT id,jd_version_id,status,created_at FROM assessments WHERE application_id=? ORDER BY created_at DESC",(identity,))
        return row

    def classify(self,identity,role_id):
        application=self.get_application(identity)
        if application["role_id"]:
            raise BusinessError("ROLE_ALREADY_ASSIGNED","已有岗位的申请不改写历史；请将原件作为另一个岗位的新投递导入。")
        if not self.db.one("SELECT id FROM roles WHERE id=?",(role_id,)):
            raise BusinessError("ROLE_NOT_FOUND","岗位不存在。",404)
        other=self.db.one("SELECT id FROM applications WHERE file_id=? AND role_id=?",(application["file_id"],role_id))
        if other:
            raise BusinessError("DUPLICATE_APPLICATION","同一原件已存在该岗位申请；保留待分类记录供人工核对。",409)
        self.db.execute("UPDATE applications SET role_id=?,status='QUEUED' WHERE id=?",(role_id,identity))
        self.db.event("CLASSIFIED","岗位已人工确认。",application_id=identity)

    def parse_application(self,application):
        file,path=self.storage.file(application["file_id"])
        document=self.db.one("SELECT * FROM documents WHERE file_id=?",(file["id"],))
        if not document or document["status"]=="FAILED":
            try:
                spans=extract_file(path)
                self.db.execute("INSERT INTO documents VALUES (?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET status=excluded.status,spans=excluded.spans,error=NULL,created_at=excluded.created_at",(file["id"],"PARSED",dump(spans),None,now()))
                document={"spans":dump(spans),"status":"PARSED"}
            except BusinessError as exc:
                quarantine=self.storage.path(f"quarantine/{file['id']}_{safe_filename(file['name'])}")
                if path != quarantine:
                    shutil.copyfile(path,quarantine)
                self.db.execute("INSERT INTO documents VALUES (?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET status='FAILED',error=excluded.error",(file["id"],"FAILED","[]",exc.message,now()))
                self.db.execute("UPDATE applications SET status='PARSE_FAILED' WHERE id=?",(application["id"],))
                raise
        role=self.db.one("SELECT * FROM roles WHERE id=?",(application["role_id"],)) if application["role_id"] else None
        target=self.storage.path(f"resumes/{safe_filename(role['name']) if role else '待分类'}/{file['sha256'][:16]}_{safe_filename(application['original_name'])}")
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            with target.open("xb") as output,path.open("rb") as source:
                shutil.copyfileobj(source,output)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=file["sha256"]:
            raise BusinessError("ARCHIVE_HASH","归档文件校验失败，停止该项处理。")
        if not role:
            self.db.execute("UPDATE applications SET status='WAITING_ROLE' WHERE id=?",(application["id"],))
            return None,None
        jd=self.db.one("SELECT * FROM jd_versions WHERE id=?",(role["current_version"],))
        if not jd or not jd["approved"] or json.loads(jd["missing"]):
            self.db.execute("UPDATE applications SET status='WAITING_JD' WHERE id=?",(application["id"],))
            return None,None
        return json.loads(document["spans"]),jd

    def preflight(self,mail=False):
        config=self.settings()
        missing=[]
        if mail:
            if not config["mail"]["enabled"] or not config["mail"]["account"] or not self.db.get("mail_secret"):
                missing.append("邮箱账号、授权码及启用开关")
            if not any(r["approved"] for r in self.roles()):
                missing.append("至少一个已确认的完整 JD")
        if not config["llm"]["approved"] or not self.db.get("model_secret"):
            missing.append("公司批准的模型接口及密钥")
        if not config["policy"]["confirmed"]:
            missing.append("学历和评分规则确认")
        if shutil.disk_usage(self.env.data_dir).free<100*1024*1024:
            missing.append("至少100MB可用存储空间")
        return {"ready":not missing,"missing":missing}

    def verify_document(self,identity,text,note):
        if self.run_lock.locked():
            raise BusinessError("RUN_BUSY","请等待当前任务完成后核验文字。",409)
        application=self.get_application(identity)
        if not application["document"] or application["document"]["status"]!="PARSED":
            raise BusinessError("DOCUMENT_MISSING","请先完成文档解析。")
        previous=application["document"]["spans"]
        revision=uid()
        self.db.put("document_revision:"+revision,{"file_id":application["file_id"],"previous":previous,"note":note,"created_at":now()})
        spans=[{"id":"R1","page":None,"text":text,"source":"verified","review_id":revision}]
        with self.db.connect(True) as con:
            con.execute("UPDATE documents SET spans=?,created_at=? WHERE file_id=?",(dump(spans),now(),application["file_id"]))
            con.execute("UPDATE applications SET assessment_id=NULL,status='QUEUED' WHERE file_id=?",(application["file_id"],))
        self.db.event("DOCUMENT_VERIFIED","解析文字已由用户核验；原件和旧文字保留，同原件申请加入重评。",application_id=identity)

    def start_run(self,mode,key,trigger="manual"):
        previous=self.db.get("request:"+key)
        if previous:
            return self.db.one("SELECT * FROM runs WHERE id=?",(previous,))
        if not self.run_lock.acquire(blocking=False):
            raise BusinessError("RUN_BUSY","已有任务运行中，请等待完成。",409)
        identity=uid()
        try:
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",(identity,trigger,"RUNNING","preflight",None,None,"{}",None,now(),None))
            self.db.put("request:"+key,identity)
            self.worker=threading.Thread(target=self._run,args=(identity,mode),daemon=True)
            self.worker.start()
        except BaseException:
            self.run_lock.release()
            raise
        return self.db.one("SELECT * FROM runs WHERE id=?",(identity,))

    def _run(self,run_id,mode):
        stats={"emails":0,"new_resumes":0,"duplicates":0,"archived":0,"assessed":0,"reused":0,"exceptions":0,"reports":0,"grades":{},"roles":{}}
        cutoff=datetime.now(timezone.utc)
        cursor=self.db.get("mail_cursor",{})
        start=datetime.fromisoformat(cursor["cutoff"]) if cursor.get("cutoff") else cutoff-timedelta(hours=24)
        new_cursor=None
        try:
            if mode=="mail":
                check=self.preflight(mail=True)
                if not check["ready"]:
                    raise BusinessError("PREFLIGHT","需要补齐："+"；".join(check["missing"]))
                self.db.execute("UPDATE runs SET window_start=?,window_end=?,stage='mail' WHERE id=?",(start.isoformat(),cutoff.isoformat(),run_id))
                config=self.settings()["mail"]
                max_uid=cursor.get("last_uid",0)
                with Inbox(config,self.vault.decrypt(self.db.get("mail_secret",""))) as inbox:
                    if cursor.get("uid_validity")!=inbox.uid_validity:
                        max_uid=0
                    for mail in inbox.messages(start,cutoff,cursor.get("last_uid"),cursor.get("uid_validity")):
                        max_uid=max(max_uid,int(mail["uid"]))
                        if mail.get("skipped"):
                            continue
                        stats["emails"]+=1
                        event=self.db.one("SELECT * FROM mail_events WHERE mailbox=? AND uid_validity=? AND mail_uid=?",(config["account"],inbox.uid_validity,mail["uid"]))
                        if not event:
                            event={"id":uid(),"status":"REGISTERED"}
                            self.db.execute("INSERT INTO mail_events VALUES (?,?,?,?,?,?,?)",(event["id"],config["account"],inbox.uid_validity,mail["uid"],mail.get("message_id",""),mail["received_at"],"REGISTERED"))
                        if event["status"]=="COMPLETED":
                            stats["duplicates"]+=1
                            continue
                        if mail.get("error"):
                            self._exception(run_id,mail["error"],"邮件超过大小限制，已登记待人工处理。",stats)
                        for name,data in mail.get("attachments",[]):
                            if Path(name).suffix.lower()!=".pdf":
                                self.db.event("ATTACHMENT_SKIPPED","跳过非PDF附件。",run_id=run_id)
                                continue
                            try:
                                receipt=self.ingest(data,name,mail_event_id=event["id"])
                                stats["duplicates"]+=int(receipt["duplicate"])
                            except BusinessError as exc:
                                self._exception(run_id,exc.code,exc.message,stats)
                        self.db.execute("UPDATE mail_events SET status='COMPLETED' WHERE id=?",(event["id"],))
                    new_cursor={"cutoff":cutoff.isoformat(),"last_uid":max_uid,"uid_validity":inbox.uid_validity}
            self.db.execute("UPDATE runs SET stage='processing' WHERE id=?",(run_id,))
            calls=0
            config=self.settings()
            ready=[]
            blocked=None
            for application in applications(self.db):
                if self.stop_event.is_set():
                    raise BusinessError("SHUTDOWN","服务关闭，未完成项将在后续任务继续。")
                # Existing results remain unchanged; JD/settings changes require explicit reassessment.
                if application["assessment_id"]:
                    continue
                prior=self.db.one("SELECT status FROM documents WHERE file_id=?",(application["file_id"],))
                try:
                    spans,jd=self.parse_application(application)
                    parsed=self.db.one("SELECT status FROM documents WHERE file_id=?",(application["file_id"],))
                    if parsed and parsed["status"]=="PARSED" and (not prior or prior["status"]=="FAILED"):
                        stats["new_resumes"]+=1
                    archive_key="archived:"+application["id"]
                    if application["role_id"] and not self.db.get(archive_key):
                        stats["archived"]+=1
                        stats["roles"][application["role_name"]]=stats["roles"].get(application["role_name"],0)+1
                        self.db.put(archive_key,True)
                        self.db.event("ARCHIVED","岗位归档完成，原件哈希一致。",run_id=run_id,application_id=application["id"])
                    if spans is None:
                        self._exception(run_id,"WAITING_INPUT","申请等待岗位确认或完整 JD。",stats,application["id"])
                        continue
                    ready.append((application,spans,jd))
                except BusinessError as exc:
                    self._exception(run_id,exc.code,exc.message,stats,application["id"])
            # Finish local parsing for the whole batch before any optional cloud requests.
            self.db.execute("UPDATE runs SET stage='assessment' WHERE id=?",(run_id,))
            for application,spans,jd in ready:
                try:
                    if not self.preflight()["ready"]:
                        self.db.execute("UPDATE applications SET status='WAITING_CONFIG' WHERE id=?",(application["id"],))
                        blocked="解析归档已完成；请补齐模型与评分规则后继续评估。"
                        continue
                    if blocked:
                        continue
                    if self.stop_event.is_set():
                        raise BusinessError("SHUTDOWN","服务关闭，未完成项将在后续任务继续。")
                    key=assessment_key(application,jd,config["policy"],config["llm"],spans)
                    cached=self.db.one("SELECT * FROM assessments WHERE cache_key=?",(key,))
                    if cached:
                        result=json.loads(cached["result"]);assessment_id=cached["id"];stats["reused"]+=1
                    else:
                        if calls>=config["llm"]["max_calls_per_run"]:
                            raise BusinessError("MODEL_BUDGET","本批次模型调用上限已达到，请在后续任务继续。")
                        calls+=1
                        result=assess(self.model(),application,jd,spans,config["policy"])
                        result["jd_version_id"]=jd["id"]
                        assessment_id=uid()
                        self.db.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?)",(assessment_id,application["id"],jd["id"],key,"COMPLETED",dump(result),now()))
                    self.db.execute("UPDATE applications SET assessment_id=?,status='ASSESSED',recommended_at=coalesce(recommended_at,?) WHERE id=?",(assessment_id,datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),application["id"]))
                    stats["assessed"]+=1
                    self.db.event("ASSESSED","证据校验和评分完成。",run_id=run_id,application_id=application["id"])
                    stats["grades"][result["grade"]]=stats["grades"].get(result["grade"],0)+1
                except BusinessError as exc:
                    if exc.code in {"MODEL_NOT_CONFIGURED","MODEL_FAILED","MODEL_BUDGET","POLICY_UNCONFIRMED","SHUTDOWN"}:
                        blocked=exc.message
                        self._exception(run_id,exc.code,exc.message,stats,application["id"])
                        continue
                    self.db.execute("UPDATE applications SET status=? WHERE id=?",("PARSE_FAILED" if exc.code.startswith(("PARSE","OCR","PDF","PAGE","EMPTY")) else "WAITING_REVIEW",application["id"]))
                    self._exception(run_id,exc.code,exc.message,stats,application["id"])
            self.db.execute("UPDATE runs SET stage='reports' WHERE id=?",(run_id,))
            if blocked and stats["exceptions"]==0:
                self._exception(run_id,"WAITING_CONFIG",blocked,stats)
            files=role_reports(self.db,self.storage,run_id)
            stats["reports"]=len(files)
            report=overview_export(self.db,self.storage)
            stats["overview_file_id"]=report["id"]
            with self.db.connect(True) as con:
                if new_cursor and not blocked:
                    con.execute("INSERT INTO meta VALUES ('mail_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(dump(new_cursor),))
                con.execute("UPDATE runs SET status=?,stage='complete',stats=?,error=?,finished_at=? WHERE id=?",("BLOCKED" if blocked else "SUCCESS_WITH_EXCEPTIONS" if stats["exceptions"] else "SUCCESS",dump(stats),blocked,now(),run_id))
            summary=f"本次处理{stats['emails']}封邮件，新增{stats['new_resumes']}份简历，生成{stats['reports']}份岗位推荐报告，异常{stats['exceptions']}条。"
            details=f" 成功归档{stats['archived']}份；岗位数量：{dump(stats['roles'])}；推荐分布：{dump(stats['grades'])}；总览：{report['relative_path']}。"
            stats["role_report_ids"]=[f["id"] for f in files]
            self.db.execute("UPDATE runs SET stats=? WHERE id=?",(dump(stats),run_id))
            self.db.notify(summary+details+(blocked or ""),"error" if blocked else "warning" if stats["exceptions"] else "info",run_id)
        except Exception as exc:
            code=exc.code if isinstance(exc,BusinessError) else "INTERNAL_ERROR"
            message=exc.message if isinstance(exc,BusinessError) else "任务出现内部错误；已保存完成的步骤，请查看日志或联系维护人员。"
            self.db.execute("UPDATE runs SET status='BLOCKED',stats=?,error=?,finished_at=? WHERE id=?",(dump(stats),message,now(),run_id))
            self.db.event(code,message,run_id=run_id,level="error")
            self.db.notify(message,"error",run_id)
        finally:
            self.run_lock.release()

    def _exception(self,run_id,code,message,stats,application_id=None):
        stats["exceptions"]+=1
        self.db.event(code,message,run_id=run_id,application_id=application_id,level="warning")

    def startup(self):
        self.db.execute("UPDATE runs SET status='INTERRUPTED',error='服务重启，已完成步骤保留；请继续处理待办。',finished_at=? WHERE status='RUNNING'",(now(),))
        if self.env.scheduler:
            self.scheduler_thread=threading.Thread(target=self._schedule_loop,daemon=True)
            self.scheduler_thread.start()

    def _schedule_loop(self):
        while not self.stop_event.wait(20):
            config=self.settings()["schedule"]
            if not config["enabled"]:
                continue
            local=datetime.now(ZoneInfo("Asia/Shanghai"))
            due=local.replace(hour=int(config["time"][:2]),minute=int(config["time"][3:]),second=0,microsecond=0)
            if local<due:
                continue
            day=local.date().isoformat()
            if self.db.get("schedule_day")==day or self.run_lock.locked():
                continue
            attempt=self.db.get("schedule_attempt",0)
            if time.time()-attempt<300:
                continue
            try:
                self.db.put("schedule_attempt",time.time())
                run=self.start_run("mail",uid(),"schedule")
                # One scheduled batch per day. Failures remain explicit and can be retried manually.
                self.db.put("schedule_day",day)
                self.db.event("SCHEDULE_STARTED","北京时间计划任务已触发。",run_id=run["id"])
            except BusinessError:
                continue

    def shutdown(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=10)
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=2)
        if not self.worker or not self.worker.is_alive():
            self.instance_lock.close()
