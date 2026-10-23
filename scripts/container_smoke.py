"""CI integration check: real Linux image, HTTP auth, Chinese OCR, restart data."""
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.request


def docker(*args):
    return subprocess.check_output(['docker',*args],text=True).strip()


def wait():
    for _ in range(45):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18088/healthz',timeout=2) as response:
                if response.status==200:return
        except OSError:time.sleep(1)
    raise RuntimeError('container did not become healthy')


password=secrets.token_urlsafe(32)
name='hr-assist-ci'
try:
    docker('run','-d','--name',name,'--read-only','--tmpfs','/tmp:size=268435456,mode=1777',
           '-p','127.0.0.1:18088:8000','-e','HR_ADMIN_PASSWORD='+password,'-e','HR_SECURE_COOKIE=false','-e','HR_SCHEDULER=false',
           '--mount','type=volume,destination=/state','--mount','type=volume,destination=/data','hr-assist:ci')
    wait()
    request=urllib.request.Request('http://127.0.0.1:18088/api/auth/login',data=json.dumps({'username':'admin','password':password}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request) as response:
        assert response.status==200 and 'csrf' in json.load(response)
    languages=docker('exec',name,'tesseract','--list-langs')
    assert 'chi_sim' in languages and 'eng' in languages
    # A raster-only PDF must traverse PDFium + Tesseract, not the text extractor.
    code="""
from PIL import Image,ImageDraw,ImageFont
from pathlib import Path
from hr_assist.documents import extract_file
im=Image.new('RGB',(1500,400),'white')
d=ImageDraw.Draw(im)
d.text((70,90),'Synthetic resume Python engineer 5 years experience',fill='black',font=ImageFont.load_default(size=40))
p=Path('/tmp/scan.pdf');im.save(p,'PDF',resolution=150)
spans=extract_file(p)
assert any(s['source']=='ocr' and 'Python' in s['text'] for s in spans), spans
print('OCR raster PDF passed')
"""
    print(docker('exec',name,'python','-c',code))
    docker('restart',name);wait()
    with urllib.request.urlopen(request) as response:assert response.status==200
    print('Container auth, OCR and persisted administrator passed')
finally:
    subprocess.run(['docker','rm','-fv',name],stdout=subprocess.DEVNULL,check=False)
