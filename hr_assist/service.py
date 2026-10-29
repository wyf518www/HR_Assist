from copy import deepcopy
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import shutil
import threading
import time

from .assessment import MODEL_CALL_ERRORS, CloudModel, assess, assessment_key, default_criteria, missing_jd_fields, name_looks_like_location, scoring_criteria, validate_redaction_name
from .assessment_explanations import decision_reasons
from .model_contract import format_adjustment_message
from .parallel import BatchControl, BatchStopped, bounded_candidates
from .config import DEFAULT_SETTINGS
from .db import Database, dump, now, uid
from .documents import extract_file
from .diagnostics import Diagnostics
from .mailbox import Inbox
from .instance import InstanceLock
from .jd_imports import JDImports
from .identities import Identities
from .reports import applications, empty_cells, overview_export, role_reports
from .security import Vault, password_hash
from .storage import BusinessError, Storage, safe_filename


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
        self.diagnostics=Diagnostics(environment.state_dir,self.settings)
        self.run_lock=threading.Lock()
        self.ingest_lock=threading.Lock()
        # PDF extraction, quarantine and archive writes share a short local
        # critical section. Model requests never hold this lock.
        self.document_lock=threading.RLock()
        self.identities=Identities(self)
        self.stop_event=threading.Event()
        self.worker=None
        self.scheduler_thread=None
        self.maintenance_thread=None
        self.jd_imports=JDImports(self)

    def settings(self):
        config=self.db.get("settings")
        config['llm']={**DEFAULT_SETTINGS['llm'],**config.get('llm',{})}
        config['diagnostics']={**DEFAULT_SETTINGS['diagnostics'],**config.get('diagnostics',{})}
        return config

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
        if data.resume_concurrency is not None:
            config['llm']['resume_concurrency']=data.resume_concurrency
        if data.llm_timeout_seconds is not None:
            config["llm"]["timeout_seconds"]=data.llm_timeout_seconds
        for key in ('reasoning_effort','max_tokens'):
            field='llm_'+key
            if field in data.model_fields_set:
                config['llm'][key]=getattr(data,field)
        # Validate the merged profile before saving either settings or credentials.
        from .model_options import generation_profile
        generation_profile(config['llm'])
        config["schedule"].update(enabled=data.schedule_enabled,time=data.schedule_time)
        for key in ('enabled','retention_days','cleanup_time'):
            value=getattr(data,'diagnostics_'+key)
            if value is not None:
                config['diagnostics'][key]=value
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
        # Read connection settings and its credential from the same SQLite
        # statement so a concurrent settings save cannot mix two accounts.
        values={row['key']:json.loads(row['value']) for row in self.db.all(
            "SELECT key,value FROM meta WHERE key IN ('settings','model_secret')")}
        settings={**DEFAULT_SETTINGS['llm'],**values['settings']['llm']}
        return CloudModel(settings,self.vault.decrypt(values.get('model_secret','')))

    def model_snapshot(self,settings):
        model=self.model()
        def request_settings(value):
            return {key:item for key,item in value.items() if key not in {'resume_concurrency','max_calls_per_run'}}
        if isinstance(model,CloudModel) and request_settings(model.settings)!=request_settings(settings):
            # No request has been sent. A fresh manual run can use the new
            # configuration; never label a response with an older cache key.
            raise BusinessError('MODEL_REQUEST','准备请求期间模型配置已变更，本次未发送；请使用新配置手动继续处理。')
        return model

    @contextmanager
    def _phase(self,phase,run_id,*,update_stage=True,**fields):
        """Link operations to a batch without putting business text in diagnostics."""
        if update_stage:
            self.db.execute('UPDATE runs SET stage=? WHERE id=?',(phase,run_id))
        try:
            with self.diagnostics.operation(phase,run_id=run_id,**fields):
                yield
        except BusinessError as exc:
            if isinstance(exc,BatchStopped):
                raise
            label={'mail_fetch':'邮件获取','document_parse':'简历解析','identity':'姓名与岗位识别',
                   'assessment':'简历评分','report_export':'报告生成','archive':'岗位归档',
                   'identity_review':'姓名与岗位核对'}.get(phase,'任务处理')
            raise BusinessError(exc.code,f'{label}阶段：{exc.message}',exc.status,diagnostics=exc.diagnostics) from exc

    def roles(self, *, summary=False, identity=None):
        where=' WHERE r.id=?' if identity is not None else ''
        args=(identity,) if identity is not None else ()
        if summary:
            rows=self.db.all('SELECT r.id,r.name,r.active,r.is_sales,r.current_version FROM roles r'+where+' ORDER BY r.active DESC,r.name',args)
            for row in rows:
                row['active']=bool(row['active'])
            return rows
        rows=self.db.all("SELECT r.*,j.text jd_text,j.fields,j.criteria,j.approved,j.missing,j.source_file_id,"
                         "(SELECT COUNT(*) FROM applications a WHERE a.role_id=r.id) application_count,"
                         "(SELECT COUNT(*) FROM assessments a JOIN jd_versions v ON a.jd_version_id=v.id WHERE v.role_id=r.id) assessment_count,"
                         "(SELECT COUNT(*) FROM jd_versions v WHERE v.role_id=r.id) version_count "
                         "FROM roles r LEFT JOIN jd_versions j ON r.current_version=j.id"+where+" ORDER BY r.active DESC,r.name",args)
        for row in rows:
            row['active']=bool(row['active'])
            row['can_delete']=not row['application_count'] and not row['assessment_count']
            for key in ("fields","criteria","missing"):
                row[key]=json.loads(row[key]) if row[key] else ({} if key=="fields" else [])
        return rows

    def save_role(self,data,identity=None,*,import_id=None,expected_version=None):
        if self.run_lock.locked():
            raise BusinessError("RUN_BUSY","请等待当前任务完成后修改岗位 JD。",409)
        fields=data.fields.model_dump()
        missing=missing_jd_fields(fields)
        if not data.jd_text.strip():
            missing.append("JD 原文")
        if data.approved and missing:
            raise BusinessError("JD_INCOMPLETE","JD 缺失项："+"、".join(missing))
        if data.source_file_id:
            file,_=self.storage.file(data.source_file_id)
            if file["kind"]!="jd":
                raise BusinessError("JD_FILE","只能关联已上传的 JD 文件。")
        updating=identity is not None
        identity=identity or uid()
        version=uid()
        criteria=scoring_criteria([c.model_dump() for c in data.criteria],fields) if data.criteria else default_criteria(fields)
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
                exists=con.execute("SELECT id,current_version FROM roles WHERE id=?",(identity,)).fetchone()
                if updating and not exists:
                    raise BusinessError("ROLE_NOT_FOUND","岗位已被删除，请返回岗位列表。",404)
                if import_id and expected_version and (not exists or exists['current_version']!=expected_version):
                    raise BusinessError("JD_VERSION_CONFLICT","岗位已被修改，请返回导入列表并重新核对当前版本。",409)
                if exists:
                    con.execute("UPDATE roles SET name=?,is_sales=? WHERE id=?",(data.name,data.is_sales,identity))
                else:
                    con.execute("INSERT INTO roles(id,name,is_sales,current_version,created_at) VALUES (?,?,?,?,?)",(identity,data.name,data.is_sales,None,now()))
                con.execute("INSERT INTO jd_versions VALUES (?,?,?,?,?,?,?,?,?)",(version,identity,data.jd_text,dump(fields),dump(criteria),data.approved,dump(missing),data.source_file_id,now()))
                con.execute("UPDATE roles SET current_version=? WHERE id=?",(version,identity))
                if import_id:
                    con.execute("UPDATE jd_imports SET status='SAVED',role_id=?,version_id=?,updated_at=? WHERE id=?",(identity,version,now(),import_id))
        except __import__("sqlite3").IntegrityError as exc:
            raise BusinessError("ROLE_DUPLICATE","岗位名称已存在。") from exc
        self.db.event("JD_SAVED","已保存新的 JD 版本。")
        return next(r for r in self.roles() if r["id"]==identity)

    def change_role(self, identity, data, *, delete=False):
        if not self.run_lock.acquire(blocking=False):
            raise BusinessError("RUN_BUSY","请等待当前任务结束后删除或停用岗位。",409)
        try:
            with self.ingest_lock, self.jd_imports.lock, self.db.connect(True) as con:
                role=con.execute("SELECT * FROM roles WHERE id=?",(identity,)).fetchone()
                if not role:
                    raise BusinessError("ROLE_NOT_FOUND","岗位不存在或已被删除。",404)
                if role['current_version']!=data.expected_version:
                    raise BusinessError("JD_VERSION_CONFLICT","岗位已被修改，请刷新列表后重新确认。",409)
                if delete:
                    linked=con.execute("SELECT 1 FROM applications WHERE role_id=? LIMIT 1",(identity,)).fetchone()
                    scored=con.execute("SELECT 1 FROM assessments a JOIN jd_versions j ON a.jd_version_id=j.id WHERE j.role_id=? LIMIT 1",(identity,)).fetchone()
                    if linked or scored:
                        raise BusinessError("ROLE_IN_USE","岗位已关联候选人或评分，不能删除；请使用停用以保留历史依据。",409)
                    con.execute("UPDATE jd_imports SET status='ROLE_DELETED',role_id=NULL,version_id=NULL,updated_at=? WHERE role_id=?",(now(),identity))
                    con.execute("DELETE FROM jd_versions WHERE role_id=?",(identity,))
                    con.execute("DELETE FROM roles WHERE id=?",(identity,))
                    code,message='ROLE_DELETED',f"岗位 {identity} 及未使用的 JD 版本已删除；原始文件和导入记录保留。"
                else:
                    con.execute("UPDATE roles SET active=? WHERE id=?",(int(data.active),identity))
                    code='ROLE_ENABLED' if data.active else 'ROLE_DISABLED'
                    message=f"岗位 {identity} 已{'启用' if data.active else '停用'}，历史 JD 和招聘记录保留。"
            self.db.event(code,message)
            return {'id':identity,'deleted':delete} if delete else next(r for r in self.roles() if r['id']==identity)
        finally:
            self.run_lock.release()

    def upload_jd(self,data,name):
        if Path(name).suffix.lower() not in {".pdf",".docx",".txt"}:
            raise BusinessError("JD_FORMAT","JD 支持 PDF、DOCX、UTF-8 TXT。")
        file=self.storage.put(data,name,"jd")
        _,path=self.storage.file(file["id"])
        spans=extract_file(path)
        return {"file_id":file["id"],"text":"\n\n".join(s["text"] for s in spans),"spans":spans}

    def ingest(self,data,name,role_id=None,mail_event_id=None,subject=''):
        with self.ingest_lock:
            return self._ingest(data,name,role_id,mail_event_id,subject)

    def _ingest(self,data,name,role_id=None,mail_event_id=None,subject=''):
        if Path(name).suffix.lower()!=".pdf":
            raise BusinessError("RESUME_FORMAT","简历只接受 PDF 文件。")
        if len(data)>self.env.max_upload_bytes:
            raise BusinessError("UPLOAD_LIMIT","单个附件不能超过20MB。")
        if not data[:1024].lstrip().startswith(b"%PDF-"):
            raise BusinessError("INVALID_PDF","附件后缀为PDF，但文件内容不是PDF。")
        # Filename structure is not an identity contract. Preserve it as model
        # input; never assign a city/name/role by splitting separators here.
        if role_id:
            role=self.db.one("SELECT id,active FROM roles WHERE id=?",(role_id,))
            if not role:
                raise BusinessError("ROLE_NOT_FOUND","岗位不存在。",404)
            if not role['active']:
                raise BusinessError("ROLE_INACTIVE","该岗位已停用，请选择启用中的岗位。",409)
        file=self.storage.put(data,name,"resumes",folder="temporary",deduplicate=True)
        subject=subject[:2000]
        existing=None
        if role_id:
            existing=self.db.one("SELECT id FROM applications WHERE file_id=? AND role_id=?",(file['id'],role_id))
        else:
            # A receipt identifies a replay of one mail attachment. Matching
            # bytes alone cannot merge separate applications for different jobs.
            if mail_event_id:
                existing=self.db.one("SELECT a.id FROM receipts x JOIN applications a ON a.id=x.application_id WHERE x.mail_event_id=? AND x.sha256=?",(mail_event_id,file['sha256']))
            if not existing:
                for candidate in self.db.all("SELECT id FROM applications WHERE file_id=? AND original_name=? ORDER BY created_at,id",(file['id'],name)):
                    known=self.db.get('identity_sources:'+candidate['id'],{})
                    if not subject or subject in known.get('subjects',[]):
                        existing=candidate
                        break
        identity=existing["id"] if existing else uid()
        with self.db.connect(True) as con:
            if not existing:
                con.execute("INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?)",(identity,file["id"],role_id,"待识别",name,"QUEUED",None,None,now()))
                con.execute("INSERT INTO followups VALUES (?,?,0,?)",(identity,dump(empty_cells()),now()))
                if role_id:
                    con.execute('INSERT INTO meta VALUES (?,?)',('candidate_role_review:'+identity,dump({'role_id':role_id,'created_at':now()})))
            if mail_event_id:
                con.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?,?)",(uid(),mail_event_id,file["sha256"],file["id"],identity,"DUPLICATE" if existing else "REGISTERED"))
        sources=self.db.get('identity_sources:'+identity,{'subjects':[],'filenames':[]})
        if subject and subject not in sources['subjects']:
            sources['subjects'].append(subject)
        if name not in sources['filenames']:
            sources['filenames'].append(name[:1000])
        self.db.put('identity_sources:'+identity,{k:v[-20:] for k,v in sources.items()})
        self.db.event("DUPLICATE" if existing else "RESUME_REGISTERED","重复原件已关联既有申请。" if existing else "PDF 原件已登记。",application_id=identity)
        return {"id":identity,"duplicate":bool(existing),"file_id":file["id"]}

    def get_application(self,identity):
        rows=applications(self.db,identity=identity)
        row=rows[0] if rows else None
        if not row:
            raise BusinessError("APPLICATION_NOT_FOUND","申请不存在。",404)
        if row.get('assessment'):
            # Explain the stored decision without changing its historical score,
            # policy result or manually maintained follow-up information.
            row['assessment']['decision_reasons']=decision_reasons(row['assessment'])
        document=self.db.one("SELECT * FROM documents WHERE file_id=?",(row["file_id"],))
        row["document"]={**document,"spans":json.loads(document["spans"])} if document else None
        row["name_confirmed"]=self.name_is_confirmed(row)
        row['identity_complete']=self.identities.complete(row)
        row["name_review_required"]=bool(not row["name_confirmed"] and not row['identity_complete'] and document and name_looks_like_location(row["document"]["spans"],row["name"]))
        row["history"]=self.db.all("SELECT id,jd_version_id,status,created_at FROM assessments WHERE application_id=? ORDER BY created_at DESC",(identity,))
        return row

    def name_is_confirmed(self,application):
        review=self.db.get("candidate_name_review:"+application["id"],{})
        return review.get("name")==application["name"]

    def review_candidate_name(self,identity,data):
        if not self.run_lock.acquire(blocking=False):
            raise BusinessError("RUN_BUSY","请等待当前任务完成后核对姓名。",409)
        try:
            with self.ingest_lock, self.db.connect(True) as con:
                row=con.execute("SELECT * FROM applications WHERE id=?",(identity,)).fetchone()
                if row is None:
                    raise BusinessError("APPLICATION_NOT_FOUND","申请不存在。",404)
                if row["name"]!=data.expected_name:
                    raise BusinessError("NAME_CHANGED","姓名已被修改，请刷新页面后重新核对。",409)
                review={"name":data.name,"previous_name":row["name"],"application_id":identity,"created_at":now()}
                con.execute("INSERT INTO meta VALUES (?,?)",("candidate_name_revision:"+uid(),dump(review)))
                con.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            ("candidate_name_review:"+identity,dump(review)))
                con.execute("UPDATE applications SET name=? WHERE id=?",(data.name,identity))
            self.db.event("CANDIDATE_NAME_VERIFIED","候选人姓名已人工核对；已有评分和人工跟进保留。需更新评分时请明确申请重新评估。",application_id=identity)
            return {"ok":True}
        finally:
            self.run_lock.release()

    def classify(self,identity,role_id):
        with self.ingest_lock:
            return self._classify(identity,role_id)

    def _classify(self,identity,role_id):
        if self.run_lock.locked():
            raise BusinessError("RUN_BUSY","请等待当前任务完成后确认岗位。",409)
        application=self.get_application(identity)
        if application["role_id"]:
            raise BusinessError("ROLE_ALREADY_ASSIGNED","已有岗位的申请不改写历史；请将原件作为另一个岗位的新投递导入。")
        role=self.db.one("SELECT id,active FROM roles WHERE id=?",(role_id,))
        if not role:
            raise BusinessError("ROLE_NOT_FOUND","岗位不存在。",404)
        if not role['active']:
            raise BusinessError("ROLE_INACTIVE","该岗位已停用，请选择启用中的岗位。",409)
        other=self.db.one("SELECT id FROM applications WHERE file_id=? AND role_id=?",(application["file_id"],role_id))
        if other:
            raise BusinessError("DUPLICATE_APPLICATION","同一原件已存在该岗位申请；保留待分类记录供人工核对。",409)
        self.db.execute("UPDATE applications SET role_id=?,status='QUEUED' WHERE id=?",(role_id,identity))
        self.db.put('candidate_role_review:'+identity,{'role_id':role_id,'created_at':now()})
        self.db.event("CLASSIFIED","岗位已人工确认。",application_id=identity)

    def parse_document(self,application,update_status=True):
        with self.document_lock:
            return self._parse_document(application,update_status)

    def _parse_document(self,application,update_status=True):
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
                if update_status:
                    self.db.execute("UPDATE applications SET status='PARSE_FAILED' WHERE id=?",(application["id"],))
                raise
        return json.loads(document['spans'])

    def parse_application(self,application):
        with self.document_lock:
            return self._parse_application(application)

    def _parse_application(self,application):
        spans=self.parse_document(application)
        file,path=self.storage.file(application['file_id'])
        role=self.db.one("SELECT * FROM roles WHERE id=?",(application["role_id"],)) if application["role_id"] else None
        target=self.storage.path(f"resumes/{safe_filename(role['name']) if role else '待分类'}/{file['sha256'][:16]}_{safe_filename(application['original_name'])}")
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            staging=self.storage.path(f"temporary/{uid()}.archive-part")
            with staging.open("xb") as output,path.open("rb") as source:
                shutil.copyfileobj(source,output)
                output.flush()
                os.fsync(output.fileno())
            if hashlib.sha256(staging.read_bytes()).hexdigest()!=file["sha256"]:
                raise BusinessError("ARCHIVE_HASH","归档暂存文件校验失败，原件保留。")
            os.replace(staging,target)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=file["sha256"]:
            raise BusinessError("ARCHIVE_HASH","归档文件校验失败，停止该项处理。")
        if not role:
            self.db.execute("UPDATE applications SET status='WAITING_ROLE' WHERE id=?",(application["id"],))
            return None,None
        if not role['active']:
            self.db.execute("UPDATE applications SET status='ROLE_INACTIVE' WHERE id=?",(application['id'],))
            return None,None
        jd=self.db.one("SELECT * FROM jd_versions WHERE id=?",(role["current_version"],))
        if not jd or not jd["approved"] or json.loads(jd["missing"]):
            self.db.execute("UPDATE applications SET status='WAITING_JD' WHERE id=?",(application["id"],))
            return None,None
        return spans,jd

    def preflight(self,mail=False):
        config=self.settings()
        missing=[]
        if mail:
            if not config["mail"]["enabled"] or not config["mail"]["account"] or not self.db.get("mail_secret"):
                missing.append("邮箱账号、授权码及启用开关")
            if not self.db.one('SELECT 1 FROM roles r JOIN jd_versions j ON j.id=r.current_version WHERE j.approved=1 LIMIT 1'):
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
            con.execute("DELETE FROM meta WHERE key IN (SELECT 'identity_applied:'||id FROM applications WHERE file_id=?)",(application['file_id'],))
        self.db.event("DOCUMENT_VERIFIED","解析文字已由用户核验；原件和旧文字保留，同原件申请加入重评。",application_id=identity)

    def start_run(self,mode,key,trigger="manual",application_ids=None,force_identity=False):
        previous=self.db.get("request:"+key)
        if previous:
            return self.db.one("SELECT * FROM runs WHERE id=?",(previous,))
        if not self.run_lock.acquire(blocking=False):
            raise BusinessError("RUN_BUSY","已有任务运行中，请等待完成。",409)
        identity=uid()
        try:
            batch_settings=self.settings()
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",(identity,trigger,"RUNNING","preflight",None,None,"{}",None,now(),None))
            self.db.put("request:"+key,identity)
            self.worker=threading.Thread(target=self._run_identities if mode=='identity' else self._run,args=(identity,application_ids,force_identity,batch_settings) if mode=='identity' else (identity,mode,batch_settings),daemon=True)
            self.worker.start()
        except BaseException:
            self.run_lock.release()
            raise
        return self.db.one("SELECT * FROM runs WHERE id=?",(identity,))

    def _run(self,run_id,mode,batch_settings):
        stats={"emails":0,"new_resumes":0,"duplicates":0,"archived":0,"assessed":0,"reused":0,"exceptions":0,"reports":0,"grades":{g:0 for g in ("强烈推荐","推荐","待定","不推荐")},"roles":{}}
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
                with self._phase('mail_fetch',run_id):
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
                                    receipt=self.ingest(data,name,mail_event_id=event["id"],subject=mail.get('subject',''))
                                    stats["duplicates"]+=int(receipt["duplicate"])
                                except BusinessError as exc:
                                    self._exception(run_id,exc.code,exc.message,stats)
                            self.db.execute("UPDATE mail_events SET status='COMPLETED' WHERE id=?",(event["id"],))
                        new_cursor={"cutoff":cutoff.isoformat(),"last_uid":max_uid,"uid_validity":inbox.uid_validity}
            self.db.execute("UPDATE runs SET stage='processing' WHERE id=?",(run_id,))
            config=batch_settings
            control=BatchControl(config['llm']['max_calls_per_run'],config['llm']['resume_concurrency'],self.stop_event)
            stats['resume_concurrency']=control.concurrency
            ready=[]
            blocked=None
            for application in applications(self.db):
                if self.stop_event.is_set():
                    raise BusinessError('SHUTDOWN','服务关闭，未完成项将在后续任务继续。')
                if application['assessment_id']:
                    continue
                prior=self.db.one('SELECT status FROM documents WHERE file_id=?',(application['file_id'],))
                if not prior:
                    stats['new_resumes']+=1
                try:
                    with self._phase('document_parse',run_id,application_id=application['id'],file_id=application['file_id']):
                        self.parse_document(application)
                    # A human-selected job is already safe to archive locally,
                    # even if the later name/model request is unavailable.
                    if application['role_id'] and self.identities.human_role(application):
                        with self._phase('archive',run_id,application_id=application['id'],file_id=application['file_id']):
                            self.parse_application(application)
                        archive_key='archived:'+application['id']
                        if not self.db.get(archive_key):
                            stats['archived']+=1
                            stats['roles'][application['role_name']]=stats['roles'].get(application['role_name'],0)+1
                            self.db.put(archive_key,True)
                            self.db.event('ARCHIVED','岗位归档完成，原件哈希一致。',run_id=run_id,application_id=application['id'])
                    ready.append(application)
                except BusinessError as exc:
                    self._exception(run_id,exc.code,exc.message,stats,application['id'])
            self.db.execute("UPDATE runs SET stage='identity_and_assessment' WHERE id=?",(run_id,))
            process=lambda app:self._process_candidate(run_id,app,config,control)
            with closing(bounded_candidates(ready,process,control)) as completed_candidates:
                for completed in completed_candidates:
                    self._merge_candidate_stats(stats,completed)
                    stats['model_calls']=control.calls
                    self.db.execute('UPDATE runs SET stats=? WHERE id=?',(dump(stats),run_id))
            stats['model_calls']=control.calls
            if control.error:
                blocked=control.error.message
                stats['failed_phase']=control.phase or 'identity_and_assessment'
            self.db.execute("UPDATE runs SET stage='reports' WHERE id=?",(run_id,))
            if blocked and stats["exceptions"]==0:
                self._exception(run_id,control.error.code,blocked,stats)
            with self._phase('report_export',run_id):
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
            stage=self.db.one('SELECT stage FROM runs WHERE id=?',(run_id,))['stage']
            stats['failed_phase']=stage
            self.diagnostics.failure(stage,exc,run_id=run_id)
            code=exc.code if isinstance(exc,BusinessError) else "INTERNAL_ERROR"
            message=exc.message if isinstance(exc,BusinessError) else "任务出现内部错误；已保存完成的步骤，请查看日志或联系维护人员。"
            self.db.execute("UPDATE runs SET status='BLOCKED',stats=?,error=?,finished_at=? WHERE id=?",(dump(stats),message,now(),run_id))
            self.db.event(code,message,run_id=run_id,level="error")
            self.db.notify(message,"error",run_id)
        finally:
            self.run_lock.release()

    @staticmethod
    def _merge_candidate_stats(stats,completed):
        # Only the batch owner writes its aggregate; worker counters are local.
        for key,value in completed.items():
            if isinstance(value,dict):
                for label,count in value.items():
                    stats[key][label]=stats[key].get(label,0)+count
            else:
                stats[key]=stats.get(key,0)+value

    def _process_candidate(self,run_id,application,config,control):
        stats={'archived':0,'assessed':0,'reused':0,'exceptions':0,'grades':{},'roles':{}}
        phase='identity'
        fields={'application_id':application['id'],'file_id':application['file_id'],'update_stage':control.concurrency==1}
        try:
            control.check()
            with self._phase(phase,run_id,**fields):
                application=self.identities.resolve(application,control.reserve)
            phase='archive'
            with self._phase(phase,run_id,**fields):
                spans,jd=self.parse_application(application)
            archive_key='archived:'+application['id']
            if application['role_id'] and not self.db.get(archive_key):
                stats['archived']+=1
                stats['roles'][application['role_name']]=1
                self.db.put(archive_key,True)
                self.db.event('ARCHIVED','岗位归档完成，原件哈希一致。',run_id=run_id,application_id=application['id'])
            if spans is None:
                self._exception(run_id,'WAITING_INPUT','申请等待岗位启用、岗位确认或完整 JD。',stats,application['id'])
                return stats
            if not self.preflight()['ready']:
                self.db.execute("UPDATE applications SET status='WAITING_CONFIG' WHERE id=?",(application['id'],))
                control.stop(BusinessError('WAITING_CONFIG','解析归档已完成；请补齐模型与评分规则后继续评估。'),phase)
                return stats
            phase='assessment'
            with self._phase(phase,run_id,**fields,span_count=len(spans),input_characters=sum(len(s['text']) for s in spans)):
                application['name_confirmed']=self.name_is_confirmed(application) or self.identities.complete(application)
                validate_redaction_name(spans,application['name'],application['name_confirmed'])
                model_settings=deepcopy(self.settings()['llm'])
                model=self.model_snapshot(model_settings)
                key=assessment_key(application,jd,config['policy'],model_settings,spans)
                cached=self.db.one('SELECT * FROM assessments WHERE cache_key=?',(key,))
                if cached:
                    result=json.loads(cached['result']);assessment_id=cached['id'];stats['reused']+=1
                else:
                    result=assess(model,application,jd,spans,config['policy'],db=self.db,reserve=control.reserve)
                    result['jd_version_id']=jd['id']
                    assessment_id=uid()
                    self.db.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?)',(assessment_id,application['id'],jd['id'],key,'COMPLETED',dump(result),now()))
            self.db.execute("UPDATE applications SET assessment_id=?,status='ASSESSED',recommended_at=coalesce(recommended_at,?) WHERE id=?",(assessment_id,datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),application['id']))
            stats['assessed']+=1
            if result.get('format_adjustments'):
                self.db.event('MODEL_FORMAT_NORMALIZED',format_adjustment_message(result['format_adjustments']),run_id=run_id,application_id=application['id'])
            for warning in result.get('validation_warnings',[]):
                self._exception(run_id,warning['code'],warning['message'],stats,application['id'])
            self.db.event('ASSESSED','暂计分完成，存在待核验项，请人工核验。' if result.get('provisional') else '证据校验和评分完成。',run_id=run_id,application_id=application['id'])
            stats['grades'][result['grade']]=1
        except BatchStopped:
            pass
        except BusinessError as exc:
            if exc.code in MODEL_CALL_ERRORS | {'MODEL_BUDGET','POLICY_UNCONFIRMED','SHUTDOWN'}:
                control.stop(exc,phase)
            else:
                self.db.execute('UPDATE applications SET status=? WHERE id=?',('WAITING_IDENTITY' if exc.code.startswith('IDENTITY_') else 'PARSE_FAILED' if exc.code.startswith(('PARSE','OCR','PDF','PAGE','EMPTY')) else 'WAITING_REVIEW',application['id']))
            self._exception(run_id,exc.code,exc.message,stats,application['id'])
        except Exception as exc:
            control.stop(BusinessError('INTERNAL_ERROR','任务出现内部错误；已保存完成的步骤，请查看日志或联系维护人员。'),phase)
            self.diagnostics.failure(phase,exc,run_id=run_id,application_id=application['id'],file_id=application['file_id'])
            self._exception(run_id,'INTERNAL_ERROR','任务出现内部错误；已保存完成的步骤，请查看日志或联系维护人员。',stats,application['id'])
        return stats

    def _run_identities(self,run_id,application_ids,force,batch_settings):
        stats={'identity_analyzed':0,'identity_reused':0,'assessed':0,'exceptions':0}
        try:
            llm=batch_settings['llm']
            control=BatchControl(llm['max_calls_per_run'],llm['resume_concurrency'],self.stop_event)
            stats['resume_concurrency']=control.concurrency
            rows=applications(self.db)
            if application_ids:
                identities=set(application_ids)
                if not identities <= {r['id'] for r in rows}:
                    raise BusinessError('APPLICATION_NOT_FOUND','部分申请已不存在，请刷新列表。',404)
                rows=[r for r in rows if r['id'] in identities]
            self.db.execute("UPDATE runs SET stage='identity_review' WHERE id=?",(run_id,))
            process=lambda app:self._process_identity(run_id,app,control,force)
            with closing(bounded_candidates(rows,process,control)) as completed_candidates:
                for completed in completed_candidates:
                    self._merge_candidate_stats(stats,completed)
                    stats['model_calls']=control.calls
                    self.db.execute('UPDATE runs SET stats=? WHERE id=?',(dump(stats),run_id))
            stats['model_calls']=control.calls
            if control.error:
                raise control.error
            self.db.execute("UPDATE runs SET status=?,stage='complete',stats=?,finished_at=? WHERE id=?",('SUCCESS_WITH_EXCEPTIONS' if stats['exceptions'] else 'SUCCESS',dump(stats),now(),run_id))
            self.db.notify(f"姓名与岗位核对：生成{stats['identity_analyzed']}条建议，复用{stats['identity_reused']}条，异常{stats['exceptions']}条。请在核对页面确认，当前记录未改动。",'info',run_id)
        except Exception as exc:
            stage=self.db.one('SELECT stage FROM runs WHERE id=?',(run_id,))['stage']
            stats['failed_phase']=stage
            self.diagnostics.failure(stage,exc,run_id=run_id)
            code=exc.code if isinstance(exc,BusinessError) else 'INTERNAL_ERROR'
            message=exc.message if isinstance(exc,BusinessError) else '姓名与岗位核对出现内部错误，已完成的建议保留，当前记录未改动。'
            self.db.execute("UPDATE runs SET status='BLOCKED',stats=?,error=?,finished_at=? WHERE id=?",(dump(stats),message,now(),run_id))
            self.db.event(code,message,run_id=run_id,level='error')
            self.db.notify(message,'error',run_id)
        finally:
            self.run_lock.release()

    def _process_identity(self,run_id,app,control,force):
        stats={'identity_analyzed':0,'identity_reused':0,'exceptions':0}
        calls=0
        def reserve():
            nonlocal calls
            control.reserve()
            calls+=1
        try:
            control.check()
            previous=self.identities.proposal(app['id'])
            marker=self.db.get('identity_applied:'+app['id'],{})
            if not force and previous and marker.get('proposal_id')==previous['id'] and self.identities.complete(app) and not self.db.get('identity_pending:'+app['id']):
                stats['identity_reused']+=1
                return stats
            with self._phase('identity_review',run_id,application_id=app['id'],file_id=app['file_id'],update_stage=False):
                self.identities.analyze(app,reserve,force=force)
            stats['identity_analyzed' if calls else 'identity_reused']+=1
            self.db.event('IDENTITY_PROPOSED','大模型已生成姓名与应聘岗位核对建议，当前记录未改动。',run_id=run_id,application_id=app['id'])
        except BatchStopped:
            pass
        except BusinessError as exc:
            if exc.code in MODEL_CALL_ERRORS | {'MODEL_BUDGET','SHUTDOWN'}:
                control.stop(exc,'identity_review')
            self._exception(run_id,exc.code,exc.message,stats,app['id'])
        except Exception as exc:
            control.stop(BusinessError('INTERNAL_ERROR','姓名与岗位核对出现内部错误，已完成的建议保留，当前记录未改动。'),'identity_review')
            self.diagnostics.failure('identity_review',exc,run_id=run_id,application_id=app['id'],file_id=app['file_id'])
            self._exception(run_id,'INTERNAL_ERROR','姓名与岗位核对出现内部错误，已完成的建议保留，当前记录未改动。',stats,app['id'])
        return stats

    def _exception(self,run_id,code,message,stats,application_id=None):
        stats["exceptions"]+=1
        self.db.event(code,message,run_id=run_id,application_id=application_id,level="warning")

    def startup(self):
        self.db.execute("UPDATE runs SET status='INTERRUPTED',error='服务重启，已完成步骤保留；请继续处理待办。',finished_at=? WHERE status='RUNNING'",(now(),))
        self.migrate_no_availability()
        self.migrate_model_timeout()
        self.jd_imports.startup()
        self.diagnostics.cleanup_due()
        # Maintenance remains active even when daily recruitment is disabled.
        self.maintenance_thread=threading.Thread(target=self._maintenance_loop,daemon=True)
        self.maintenance_thread.start()
        if self.env.scheduler:
            self.scheduler_thread=threading.Thread(target=self._schedule_loop,daemon=True)
            self.scheduler_thread.start()

    def migrate_model_timeout(self):
        """Replace the old hidden default once; preserve later user choices and custom values."""
        with self.db.connect(True) as con:
            if con.execute("SELECT 1 FROM meta WHERE key='migration:model-timeout-v2'").fetchone():
                return
            config=json.loads(con.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
            if config["llm"].get("timeout_seconds") in (None, 90):
                config["llm"]["timeout_seconds"]=300
                con.execute("UPDATE meta SET value=? WHERE key='settings'",(dump(config),))
            con.execute("INSERT INTO meta VALUES ('migration:model-timeout-v2','true')")

    def migrate_no_availability(self):
        """Create successor JD versions; historical assessments keep their original rubric."""
        changed=0
        with self.db.connect(True) as con:
            rows=con.execute("SELECT j.* FROM roles r JOIN jd_versions j ON r.current_version=j.id").fetchall()
            for row in rows:
                fields=json.loads(row['fields'])
                criteria=scoring_criteria(json.loads(row['criteria']),fields)
                missing=missing_jd_fields(fields)+([] if row['text'].strip() else ['JD 原文'])
                if criteria==json.loads(row['criteria']) and missing==json.loads(row['missing']):
                    continue
                version=uid()
                con.execute("INSERT INTO jd_versions VALUES (?,?,?,?,?,?,?,?,?)",(version,row['role_id'],row['text'],dump(fields),dump(criteria),bool(row['approved']) and not missing,dump(missing),row['source_file_id'],now()))
                con.execute("UPDATE roles SET current_version=? WHERE id=?",(version,row['role_id']))
                changed+=1
        if changed:
            message=f"已为{changed}个岗位建立不考察工作地点/base地和到岗时间的 JD 版本；默认地点分值转入经验年限（12分）。历史评分保留，请申请重新评估后使用新规则。"
            self.db.event("JD_LOCATION_RETIRED",message)
            self.db.notify(message)
        return changed

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

    def _maintenance_loop(self):
        while not self.stop_event.wait(20):
            self.diagnostics.cleanup_due()

    def shutdown(self):
        self.stop_event.set()
        self.jd_imports.wake.set()
        if self.jd_imports.thread:
            self.jd_imports.thread.join(timeout=10)
        if self.worker:
            self.worker.join(timeout=10)
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=2)
        if self.maintenance_thread:
            self.maintenance_thread.join(timeout=2)
        if (not self.worker or not self.worker.is_alive()) and (not self.jd_imports.thread or not self.jd_imports.thread.is_alive()):
            self.instance_lock.close()
