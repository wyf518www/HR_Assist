"""Explain saved decisions without re-scoring or modifying historical results."""
from copy import deepcopy


ADMISSION_POLICY_VERSION = 'admission-default-v1'


def score_grade(score):
    return '强烈推荐' if score >= 85 else '推荐' if score >= 70 else '待定' if score >= 60 else '不推荐'


def _reason(code, title, detail, action, criterion_id=None):
    value = {'code': code, 'title': title, 'detail': detail, 'action': action}
    if criterion_id is not None:
        value['criterion_id'] = criterion_id
    return value


def education_ocr(result):
    """Include legacy fact references as well as the typed education record."""
    education = result.get('education') or {}
    refs = list(education.get('evidence') or [])
    for key in ('education_level', 'admission', 'institution'):
        refs.extend((result.get('fact_evidence_refs') or {}).get(key) or [])
        ref = (result.get('fact_refs') or {}).get(key)
        if isinstance(ref, dict):
            refs.append(ref)
    return any(isinstance(ref, dict) and ref.get('source') == 'ocr' for ref in refs)


def _education_reasons(result):
    gate = result.get('education_gate')
    if gate not in {'unknown', 'fail'}:
        return []
    education = result.get('education')
    legacy = result.get('policy_version') != ADMISSION_POLICY_VERSION
    saved_detail = result.get('gate_reason') or '保存的评估记录未完整说明学历限制原因。'
    if education_ocr(result):
        return [_reason('EDUCATION_OCR_REVIEW', '学历来源含 OCR，尚待核验',
                        '学历、招生方式或院校依据中包含 OCR 识别文字；系统尚未采用这些文字确认全局学历门槛。',
                        '对照原始 PDF 核验学历、取得状态和院校名称；仅更正原件中已有文字，再申请重新评估。')]
    if not isinstance(education, dict):
        if legacy:
            return [_reason('EDUCATION_LEGACY_REVIEW', '历史学历限制需核对', saved_detail,
                            '查看历史原文与评分依据；如需采用当前规则，请申请重新评估。历史结论不会自动修改。')]
        return [_reason('EDUCATION_SOURCE_MISSING', '学历经历尚未明确',
                        '未获得可用于确认同一最高已取得学历的结构化经历和来源。',
                        '核对简历中的学历、毕业状态及院校资料；取得可追溯依据后再评估。')]
    if not education.get('evidence'):
        return [_reason('EDUCATION_SOURCE_MISSING', '学历经历缺少来源依据',
                        '模型给出了学历分类，但没有关联本次简历中的来源，不能据此通过学历门槛。',
                        '查看学历政策判断依据和原始简历，补齐可核验来源后再评估。')]
    completion = education.get('completion')
    if completion != 'attained':
        in_progress = completion == 'in_progress'
        return [_reason('EDUCATION_NOT_ATTAINED' if in_progress else 'EDUCATION_COMPLETION_UNKNOWN',
                        '所列学历尚在就读' if in_progress else '学历取得状态不明确',
                        '所列教育经历仍在就读，不能作为已取得学历。' if in_progress else '无法确认所列学历已经取得，不能把计划或在读经历当作毕业学历。',
                        '核对已取得的最高学历及毕业证明，并确认学历、院校和取得状态属于同一段经历。')]
    if education.get('degree') != 'bachelor':
        return [_reason('EDUCATION_POLICY_UNCOVERED', '学历或放宽策略需核对', saved_detail,
                        '核对最高已取得学历及当前确认的学历政策；不要按本科或硕士规则推断未覆盖情况。')]

    reasons = []
    admission = education.get('admission')
    if admission == 'non_regular':
        reasons.append(_reason('EDUCATION_ADMISSION_EXCEPTION', '本科招生方式存在明确例外',
                               '同一本科经历被识别为明确的非全日制、成人教育、自考、函授等特殊说明，未通过当前本科招生方式条件。',
                               '核对该说明是否确实属于本次本科经历及其原文依据；如识别有误，纠正后重新评估。'))
    elif admission == 'unknown' or (legacy and admission in {None, 'not_stated'}):
        reasons.append(_reason('EDUCATION_ADMISSION_LEGACY_UNKNOWN' if legacy else 'EDUCATION_ADMISSION_UNKNOWN',
                               '历史评估未确认本科招生方式' if legacy else '本科招生方式仍有歧义',
                               '该历史评估采用需明确核实招生方式的规则，当时未确认此条件；旧记录的“未知”未区分未说明与信息冲突，需要重新分析原文。' if legacy else
                               '模型未能确定招生方式说明是否属于同一本科经历，或资料存在矛盾；这不等同于单纯未注明招生方式。',
                               '如需采用当前“未说明且无明确例外则通过”的规则，请申请重新评估；历史分数和结论保留。' if legacy else
                               '查看学历政策判断依据与对应原文，核对冲突说明及其所属教育经历，再重新评估。'))
    match = education.get('institution_match')
    if match == 'not_in_catalog':
        reasons.append(_reason('EDUCATION_SCHOOL_NOT_LISTED', '本科院校未匹配已确认名录',
                               '模型复核认为该本科院校不在用户已核验的院校名录内，未通过院校条件。',
                               '核对学校完整名称、学院或校区及名录来源；仅在有依据时修订已核验名录。'))
    elif match != 'catalog':
        reasons.append(_reason('EDUCATION_SCHOOL_UNKNOWN', '本科院校名录匹配尚不明确',
                               '该历史评估未确认同一本科经历的学校对应当时已核验的院校名录，保留原有核验结论。' if legacy else
                               '无法确认同一本科经历的学校对应用户已核验名录；招生方式默认通过不会豁免院校条件。',
                               '核对本科院校全称及所属学院、校区，并对照连接与设置中的已核验院校名录。'))
    if not reasons:
        reasons.append(_reason('EDUCATION_LEGACY_REVIEW' if legacy else 'EDUCATION_POLICY_REVIEW',
                               '学历限制需要进一步核对', saved_detail,
                               '查看保存的学历依据与评估版本；如需更新结论，请明确申请重新评估。'))
    return reasons


def decision_reasons(result):
    """Return display reasons; never reapply current admission policy to history.

    New assessments persist their reasons. Older assessments are explained from
    their saved gate, verdicts and flags only; neither scores nor gates change.
    """
    if not isinstance(result, dict):
        return []
    saved = result.get('decision_reasons')
    if isinstance(saved, list) and saved and all(
            isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key].strip()
                                         for key in ('code', 'title', 'detail', 'action')) for row in saved):
        return deepcopy(saved)
    reasons = _education_reasons(result)
    for item in result.get('items') or []:
        if not isinstance(item, dict):
            continue
        verdict, hard = item.get('verdict'), item.get('hard', False)
        if verdict != 'unknown' and not (hard and verdict in {'partial', 'not_met'}):
            continue
        name = item.get('description') or item.get('id') or '评分条目'
        criterion_id = item.get('id')
        detail = item.get('reason') or '保存的评分未记录具体判定说明，请查看来源依据。'
        action = item.get('question') or '核对该条目要求、模型解读及对应原文；取得所需依据后再评估。'
        if verdict == 'unknown':
            title, code = '条目尚待核验：' + name, 'CRITERION_UNKNOWN'
            if any(ref.get('source') == 'ocr' for ref in item.get('evidence') or [] if isinstance(ref, dict)) and hard:
                detail = '该硬性条目的来源含 OCR，需先核验识别文字。' + detail
        elif verdict == 'partial':
            title, code = '硬性条件仅部分满足：' + name, 'HARD_CRITERION_PARTIAL'
            detail = '该条目被设为硬性条件；部分满足会使结果保持待定。' + detail
        else:
            title, code = '硬性条件未满足：' + name, 'HARD_CRITERION_NOT_MET'
            detail = '该条目被设为硬性条件；即使总分较高也不能直接推荐。' + detail
        reasons.append(_reason(code, title, detail, action, criterion_id))
    score = result.get('score')
    if isinstance(score, (int, float)) and not isinstance(score, bool) and 60 <= score < 70:
        reasons.append(_reason('SCORE_BAND_HOLD', '分数位于待定区间',
                               f'当前总分为 {score} 分，60—69 分按评分规则对应“待定”。',
                               '查看逐项得分和未满足的要求，再决定是否补充核验或安排面试。'))
    if result.get('grade') == '待定' and not reasons:
        reasons.append(_reason('REVIEW_REQUIRED', '保存的待定结论需要核对',
                               '该记录保留了待定结论，但未保存足够的逐项限制信息，无法准确还原具体缺项。',
                               '查看历史评分及原文；需要按当前规则确认原因时，请申请重新评估。'))
    return reasons
