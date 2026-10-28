from io import BytesIO
import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject

from hr_assist.config import Environment
from hr_assist.service import Service
from hr_assist.schemas import RoleInput, CandidateNameReview


@pytest.fixture
def environment(tmp_path):
    return Environment(tmp_path/'state',tmp_path/'data','管理员','Test-only-password-7391',False,False,'http://testserver')


@pytest.fixture
def service(environment):
    instance=Service(environment)
    yield instance
    instance.shutdown()


@pytest.fixture
def role_data():
    return dict(name='软件工程师',is_sales=True,jd_text='Python engineer, 3 years. Remote.',approved=True,
                fields=dict(education='不限',experience='3 years',location='不限',certificates='不限',availability='不限',
                            responsibilities='Build Python services',skills='Python'))


@pytest.fixture
def role(service,role_data):
    return service.save_role(RoleInput(**role_data))


def make_pdf(text='Synthetic resume Python engineer with 5 years experience and completed services projects.'):
    writer=PdfWriter()
    page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 36 720 Td ('+text.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')+') Tj ET').encode('ascii'))
    page[NameObject('/Contents')]=writer._add_object(stream)
    output=BytesIO();writer.write(output)
    return output.getvalue()


@pytest.fixture
def pdf():
    return make_pdf()


@pytest.fixture
def application(service,role,pdf):
    app=service.ingest(pdf,'【软件工程师_Test_测试】.pdf',role['id'])
    # This shared fixture represents a human-confirmed identity so assessment
    # regressions test scoring independently of the new extraction call.
    service.review_candidate_name(app['id'],CandidateNameReview(name='Test',expected_name='待识别',confirmed=True))
    return app


def run(service,mode='pending',key=None):
    from hr_assist.db import uid
    record=service.start_run(mode,key or uid())
    service.worker.join(timeout=25)
    assert not service.worker.is_alive()
    return service.db.one('SELECT * FROM runs WHERE id=?',(record['id'],))
