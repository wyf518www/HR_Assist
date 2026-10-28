from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import json
import secrets
import time

from fastapi import Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Environment
from .db import now, uid
from .reports import MANUAL_COLUMNS, application_counts, applications, followup_export, followup_import, overview_export, save_followup, save_followup_batch
from .schemas import CandidateNameReview, ClassifyInput, DocumentReview, FollowupInput, FollowupBatchInput, IdentityCorrections, JDImportConfirm, JDTextAnalysis, Login, RoleAction, RoleInput, RoleStatusInput, RunInput, SettingsInput
from .security import LoginLimiter, password_matches, token_hash
from .service import Service
from .middleware import BodyLimit
from .storage import BusinessError


def create_app(environment=None):
    environment=environment or Environment.read()
    service=Service(environment)
    limiter=LoginLimiter()

    @asynccontextmanager
    async def lifespan(app):
        service.startup()
        yield
        service.shutdown()

    app=FastAPI(title="HR Assist",version=__version__,lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.service=service
    app.add_middleware(BodyLimit,limit=environment.max_upload_bytes+1024*1024)

    @app.exception_handler(BusinessError)
    async def business_error(request,error):
        return JSONResponse({"error":{"code":error.code,"message":error.message}},status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error):
        # Pydantic's default error includes input values, including credentials. Do not return them.
        messages=[".".join(str(x) for x in e["loc"])+": "+e["msg"] for e in error.errors()]
        return JSONResponse({"error":{"code":"VALIDATION","message":"；".join(messages)}},status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request,error):
        service.diagnostics.failure('internal',error)
        service.db.event("HTTP_ERROR","请求处理出现内部错误；异常内容未写入日志。",level="error")
        return JSONResponse({"error":{"code":"INTERNAL_ERROR","message":"请求处理失败，请稍后重试或查看运行记录。"}},status_code=500)

    @app.middleware("http")
    async def headers(request,call_next):
        if request.method in {"POST","PUT","PATCH","DELETE"}:
            origin=request.headers.get("origin")
            allowed=environment.public_url or str(request.base_url).rstrip("/")
            if origin and origin!=allowed:
                return JSONResponse({"error":{"code":"ORIGIN","message":"请求来源不符合配置的工作台地址。"}},status_code=403)
            try:
                if int(request.headers.get("content-length","0"))>environment.max_upload_bytes+1024*1024:
                    return JSONResponse({"error":{"code":"UPLOAD_LIMIT","message":"上传内容不能超过20MB。"}},status_code=413)
            except ValueError:
                return JSONResponse({"error":{"code":"LENGTH","message":"无效的请求长度。"}},status_code=400)
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Frame-Options"]="DENY"
        response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        response.headers["Cache-Control"]="no-store"
        if environment.secure_cookie:
            response.headers["Strict-Transport-Security"]="max-age=31536000"
        return response

    def auth(request:Request):
        token=request.cookies.get("hr_session","")
        session=service.db.one("SELECT * FROM sessions WHERE token_hash=?",(token_hash(token),)) if token else None
        if not session or session["expires"]<time.time():
            raise BusinessError("AUTH_REQUIRED","请先登录工作台。",401)
        if request.method not in {"GET","HEAD","OPTIONS"} and not secrets.compare_digest(request.headers.get("X-CSRF-Token",""),session["csrf"]):
            raise BusinessError("CSRF","会话校验失败，请刷新后重试。",403)
        return session

    async def read_upload(file):
        contents=bytearray()
        while chunk:=await file.read(1024*1024):
            contents.extend(chunk)
            if len(contents)>environment.max_upload_bytes:
                raise BusinessError("UPLOAD_LIMIT","文件不能超过20MB。",413)
        if not contents:
            raise BusinessError("EMPTY_UPLOAD","上传文件为空。")
        return bytes(contents)

    @app.get("/healthz")
    def health():
        service.db.one("SELECT 1")
        return {"status":"ok","version":__version__}

    @app.post("/api/auth/login")
    def login(data:Login,request:Request,response:Response):
        if not limiter.allow(request.client.host if request.client else "unknown"):
            raise BusinessError("RATE_LIMIT","尝试次数过多，请10分钟后重试。",429)
        admin=service.db.get("admin")
        matches=password_matches(data.password,admin["hash"])
        if not matches or not secrets.compare_digest(data.username.encode(),admin["username"].encode()):
            raise BusinessError("LOGIN_FAILED","账号或密码不正确。",401)
        token=secrets.token_urlsafe(40);csrf=secrets.token_urlsafe(32)
        service.db.execute("DELETE FROM sessions WHERE expires<?",(time.time(),))
        service.db.execute("INSERT INTO sessions VALUES (?,?,?)",(token_hash(token),csrf,time.time()+environment.session_hours*3600))
        response.set_cookie("hr_session",token,httponly=True,secure=environment.secure_cookie,samesite="strict",max_age=environment.session_hours*3600)
        return {"username":admin["username"],"csrf":csrf}

    @app.get("/api/auth/session")
    def session(session=Depends(auth)):
        return {"username":service.db.get("admin")["username"],"csrf":session["csrf"],"version":__version__}

    @app.post("/api/auth/logout")
    def logout(response:Response,session=Depends(auth)):
        service.db.execute("DELETE FROM sessions WHERE token_hash=?",(session["token_hash"],))
        response.delete_cookie("hr_session",secure=environment.secure_cookie,httponly=True,samesite="strict")
        return {"ok":True}

    @app.get("/api/dashboard",dependencies=[Depends(auth)])
    def dashboard():
        counts=application_counts(service.db)
        counts['roles']=service.db.one('SELECT count(*) total FROM roles')['total']
        latest=service.db.one("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
        if latest:
            latest["stats"]=json.loads(latest["stats"])
        return {"counts":counts,
                "latest_run":latest,"preflight":service.preflight(mail=True),"schedule":service.settings()["schedule"],
                "notifications":service.db.all("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 20")}

    @app.get("/api/settings",dependencies=[Depends(auth)])
    def settings():
        return service.public_settings()

    @app.put("/api/settings",dependencies=[Depends(auth)])
    def save_settings(data:SettingsInput):
        if service.run_lock.locked():
            raise BusinessError("RUN_BUSY","请等当前任务结束后修改配置。",409)
        return service.save_settings(data)

    @app.post("/api/settings/preflight",dependencies=[Depends(auth)])
    def preflight():
        return service.preflight(mail=True)

    @app.get("/api/roles",dependencies=[Depends(auth)])
    def roles(summary:bool=False):
        return service.roles(summary=summary)

    @app.get("/api/roles/{identity}",dependencies=[Depends(auth)])
    def get_role(identity:str):
        rows=service.roles(identity=identity)
        if not rows:
            raise BusinessError('ROLE_NOT_FOUND','岗位不存在或已被删除。',404)
        return rows[0]

    @app.post("/api/roles",dependencies=[Depends(auth)])
    def create_role(data:RoleInput):
        return service.save_role(data)

    @app.put("/api/roles/{identity}",dependencies=[Depends(auth)])
    def update_role(identity:str,data:RoleInput):
        if not service.db.one("SELECT id FROM roles WHERE id=?",(identity,)):
            raise BusinessError("ROLE_NOT_FOUND","岗位不存在。",404)
        return service.save_role(data,identity)

    @app.post("/api/jd/upload",dependencies=[Depends(auth)])
    async def upload_jd(file:UploadFile=File(...)):
        from starlette.concurrency import run_in_threadpool
        data=await read_upload(file)
        return await run_in_threadpool(service.upload_jd,data,file.filename or "jd.pdf")

    @app.delete("/api/roles/{identity}",dependencies=[Depends(auth)])
    def delete_role(identity:str,data:RoleAction):
        return service.change_role(identity,data,delete=True)

    @app.post("/api/roles/{identity}/status",dependencies=[Depends(auth)])
    def set_role_status(identity:str,data:RoleStatusInput):
        return service.change_role(identity,data)

    @app.get("/api/applications",dependencies=[Depends(auth)])
    def list_applications(summary:bool=False):
        return applications(service.db,summary=summary)

    @app.post("/api/jd/imports",dependencies=[Depends(auth)],status_code=202)
    async def import_jd(file:UploadFile=File(...)):
        from starlette.concurrency import run_in_threadpool
        data=await read_upload(file)
        return await run_in_threadpool(service.jd_imports.enqueue,data,file.filename or "jd.pdf")

    @app.post("/api/jd/analyze-text",dependencies=[Depends(auth)])
    def analyze_jd_text(data:JDTextAnalysis):
        return service.jd_imports.analyze_text(data.text)

    @app.get("/api/jd/imports",dependencies=[Depends(auth)])
    def list_jd_imports():
        return service.jd_imports.list()

    @app.get("/api/jd/imports/{identity}",dependencies=[Depends(auth)])
    def get_jd_import(identity:str):
        return service.jd_imports.get(identity)

    @app.post("/api/jd/imports/{identity}/retry",dependencies=[Depends(auth)])
    def retry_jd_import(identity:str):
        return service.jd_imports.retry(identity)

    @app.post("/api/jd/imports/{identity}/confirm",dependencies=[Depends(auth)])
    def confirm_jd_import(identity:str,data:JDImportConfirm):
        return service.jd_imports.confirm(identity,data)

    @app.put('/api/applications/followup-batch',dependencies=[Depends(auth)])
    def update_followup_batch(data:FollowupBatchInput):
        return save_followup_batch(service.db,data)

    @app.get("/api/applications/{identity}",dependencies=[Depends(auth)])
    def get_application(identity:str):
        return service.get_application(identity)

    @app.get('/api/identities',dependencies=[Depends(auth)])
    def identity_proposals():
        return service.identities.review_rows()

    @app.post('/api/identities/apply',dependencies=[Depends(auth)])
    def identity_apply(data:IdentityCorrections):
        return service.identities.apply([i.model_dump() for i in data.items])

    @app.post("/api/resumes",dependencies=[Depends(auth)])
    async def upload_resume(file:UploadFile=File(...),role_id:str=Form(default="")):
        return service.ingest(await read_upload(file),file.filename or "简历.pdf",role_id or None)

    @app.post("/api/applications/{identity}/classify",dependencies=[Depends(auth)])
    def classify(identity:str,data:ClassifyInput):
        service.classify(identity,data.role_id)
        return {"ok":True}

    @app.post("/api/applications/{identity}/reassess",dependencies=[Depends(auth)])
    def reassess(identity:str):
        application=service.get_application(identity)
        if service.run_lock.locked():
            raise BusinessError("RUN_BUSY","已有任务运行中。",409)
        role=service.db.one("SELECT active FROM roles WHERE id=?",(application['role_id'],))
        if role and not role['active']:
            raise BusinessError("ROLE_INACTIVE","岗位已停用，请先启用岗位再申请重新评估。",409)
        service.db.execute("UPDATE applications SET assessment_id=NULL,status='QUEUED' WHERE id=?",(identity,))
        service.db.event("REASSESS_REQUESTED","已申请重新评估，历史结果和人工反馈保留。",application_id=identity)
        return {"ok":True}

    @app.post("/api/applications/{identity}/verify-document",dependencies=[Depends(auth)])
    def verify_document(identity:str,data:DocumentReview):
        service.verify_document(identity,data.text,data.note)
        return {"ok":True}

    @app.post("/api/applications/{identity}/verify-name",dependencies=[Depends(auth)])
    def verify_name(identity:str,data:CandidateNameReview):
        return service.review_candidate_name(identity,data)

    @app.put("/api/applications/{identity}/followup",dependencies=[Depends(auth)])
    def update_followup(identity:str,data:FollowupInput):
        return save_followup(service.db,identity,data)

    @app.post("/api/runs",dependencies=[Depends(auth)])
    def start_run(data:RunInput):
        return service.start_run(data.mode,data.idempotency_key,application_ids=data.application_ids,force_identity=data.force_identity)

    @app.get("/api/runs",dependencies=[Depends(auth)])
    def runs():
        rows=service.db.all("SELECT * FROM runs ORDER BY started_at DESC LIMIT 100")
        for row in rows:
            row["stats"]=json.loads(row["stats"])
        return rows

    @app.get("/api/events",dependencies=[Depends(auth)])
    def events():
        return service.db.all("SELECT * FROM events ORDER BY created_at DESC LIMIT 200")

    @app.get("/api/diagnostics",dependencies=[Depends(auth)])
    def diagnostic_status():
        return service.diagnostics.status()

    @app.get("/api/diagnostics/records",dependencies=[Depends(auth)])
    def diagnostic_records(day:str,run_id:str|None=None,limit:int=Query(default=200,ge=1,le=1000)):
        return service.diagnostics.read(day,limit=limit,run_id=run_id)

    @app.get("/api/diagnostics/download",dependencies=[Depends(auth)])
    def diagnostic_download(day:str,run_id:str|None=None):
        data=service.diagnostics.export(day,run_id=run_id)
        filename=f'diagnostic-{day}'+(f'-{run_id[:10]}' if run_id else '')+'.jsonl'
        return Response(content=data,media_type='application/x-ndjson',
                        headers={'Content-Disposition':f'attachment; filename="{filename}"'})

    @app.post("/api/notifications/{identity}/read",dependencies=[Depends(auth)])
    def notification_read(identity:str):
        service.db.execute("UPDATE notifications SET read=1 WHERE id=?",(identity,))
        return {"ok":True}

    @app.post("/api/reports/followup/export",dependencies=[Depends(auth)])
    def export_followup():
        return followup_export(service.db,service.storage)

    @app.post("/api/reports/followup/import",dependencies=[Depends(auth)])
    async def import_followup(file:UploadFile=File(...)):
        from starlette.concurrency import run_in_threadpool
        if not (file.filename or "").lower().endswith(".xlsx"):
            raise BusinessError("FORMAT","人工跟进表只接受 .xlsx。")
        return await run_in_threadpool(followup_import,service.db,service.storage,await read_upload(file),file.filename)

    @app.post("/api/reports/overview",dependencies=[Depends(auth)])
    def export_overview():
        return overview_export(service.db,service.storage)

    @app.get("/api/reports",dependencies=[Depends(auth)])
    def reports():
        return service.db.all("SELECT id,name,size,created_at FROM files WHERE kind='reports' ORDER BY created_at DESC LIMIT 100")

    @app.get("/api/feedback",dependencies=[Depends(auth)])
    def feedback():
        groups={}
        for row in applications(service.db):
            if not row["assessment"]:
                continue
            role=row["role_name"] or "待分类"
            group=groups.setdefault(role,{"role":role,"assessed":0,"with_feedback":0,"passed":0,"unknown":0,"observations":[]})
            group["assessed"]+=1
            value=str(row["manual"].get(MANUAL_COLUMNS[2]) or "").strip()
            if value in {"是","通过","已通过","否","不通过","未通过"}:
                group["with_feedback"]+=1
                group["passed"]+=int(value in {"是","通过","已通过"})
            else:
                group["unknown"]+=1
            comment=row["manual"].get("面试评价")
            if comment:
                group["observations"].append({"application_id":row["id"],"comment":comment})
        return {"roles":list(groups.values()),"policy":"第一版只统计和展示人工反馈，不自动调权或训练模型。调整评分细则须保存新的JD版本，再显式重评。"}

    @app.get("/api/files/{identity}",dependencies=[Depends(auth)])
    def download(identity:str):
        record,path=service.storage.file(identity)
        return FileResponse(path,filename=record["name"],media_type="application/octet-stream")

    static=Path(__file__).parent/"web"
    app.mount("/assets",StaticFiles(directory=static),name="assets")

    @app.get("/")
    def index():
        return FileResponse(static/"index.html")

    return app
