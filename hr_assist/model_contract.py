"""Bounded format compatibility and privacy-safe diagnostics for resume model output.

This adapter is deliberately separate from HTTP request schemas. It never fills a
missing verdict, criterion ID, fact value, or citation, and never computes evidence.
"""
from copy import deepcopy

from pydantic import ValidationError

from .schemas import ModelResult
from .storage import BusinessError


FINAL_JSON_INSTRUCTIONS = (
    "最终回答正文必须是一个完整、可直接解析的JSON对象；思考过程不能代替最终答案，不能只返回思考内容或空正文。"
    "直接输出约定的业务字段，不添加message、content、reasoning_content等包装键，不输出Markdown代码块、前后说明或分析过程。"
    "使用JSON双引号并正确转义引文中的引号、换行等字符；保留所有必填字段和完整闭合结构。"
    "除约定字段必需的取值与证据外，不重复输入原文；不要复述输出格式定义或示例。"
)

ASSESSMENT_OUTPUT_INSTRUCTIONS = FINAL_JSON_INSTRUCTIONS + (
    "每个评分条目优先选取1至3条最直接、互补的证据；复杂复合要求或任职时间核验需要更多证据时，以充分支撑结论为先，仍遵守output_schema的上限。"
    "同一条目不重复相同引文，不把整页简历当作一条证据。短引文仍须保留影响结论的时间、否定词、本人职责和限定条件，不能省略中间文字或拼接。"
    "reason通常用1至2句说明证据与要求的关系，不复述完整JD、整段引文或计算分数；question仅在需补充信息时提出一个针对性问题，否则用空字符串。"
    "精简不能遗漏任何criterion_id，也不能删除必要证据、把未知改为满足或不满足。"
)


ROOT_FIELDS = frozenset({"judgments", "facts"})
JUDGMENT_FIELDS = frozenset({"criterion_id", "verdict", "evidence", "reason", "question"})
FACT_FIELDS = frozenset({"field", "value", "evidence"})
EVIDENCE_FIELDS = frozenset({"span_id", "quote"})
SAFE_PATH_FIELDS = ROOT_FIELDS | JUDGMENT_FIELDS | FACT_FIELDS | EVIDENCE_FIELDS
VERDICT_LABELS = {"满足":"met", "部分满足":"partial", "不满足":"not_met", "未知":"unknown"}
FORMAT_LABELS = {
    "evidence_whitespace_aligned":"评分引文仅有空格或换行差异，已对齐并保存原文",
    "extra_fields_ignored":"忽略契约外字段（包括模型自行给出的分数）",
    "optional_null_to_empty":"可选空值按无信息处理",
    "judgment_evidence_object_to_list":"评分的单条证据对象转为数组",
    "fact_evidence_single_list_to_object":"事实的单元素证据数组转为对象",
    "verdict_label_normalized":"规范判定标签的语言、大小写或首尾空白",
}


def normalize_model_output(raw):
    """Return a fresh object and counts of fixed, value-free format adjustments."""
    changes={}

    def record(kind, count=1):
        changes[kind]=changes.get(kind,0)+count

    def project(value, allowed):
        if not isinstance(value,dict):
            return value
        extra=len(value.keys()-allowed)
        if extra:
            record("extra_fields_ignored",extra)
        return {key:value[key] for key in allowed if key in value}

    output=project(deepcopy(raw),ROOT_FIELDS)
    if not isinstance(output,dict):
        return output,changes
    if "facts" in output and output["facts"] is None:
        output["facts"]=[]
        record("optional_null_to_empty")
    judgments=output.get("judgments")
    # Leave over-limit collections intact so validation rejects them, never truncate.
    if isinstance(judgments,list) and len(judgments)<=60:
        output["judgments"]=[project(item,JUDGMENT_FIELDS) for item in judgments]
        for item in output["judgments"]:
            if not isinstance(item,dict):
                continue
            for key,empty in (("question",""),("evidence",[])):
                if key in item and item[key] is None:
                    item[key]=empty
                    record("optional_null_to_empty")
            verdict=item.get("verdict")
            if isinstance(verdict,str):
                normalized=VERDICT_LABELS.get(verdict.strip(),verdict.strip().lower())
                if normalized in {"met","partial","not_met","unknown"} and normalized!=verdict:
                    item["verdict"]=normalized
                    record("verdict_label_normalized")
            evidence=item.get("evidence")
            if isinstance(evidence,dict):
                evidence=item["evidence"]=[evidence]
                record("judgment_evidence_object_to_list")
            if isinstance(evidence,list) and len(evidence)<=12:
                item["evidence"]=[project(ref,EVIDENCE_FIELDS) for ref in evidence]
    facts=output.get("facts")
    if isinstance(facts,list) and len(facts)<=15:
        output["facts"]=[project(item,FACT_FIELDS) for item in facts]
        for item in output["facts"]:
            if not isinstance(item,dict):
                continue
            evidence=item.get("evidence")
            if isinstance(evidence,list) and len(evidence)==1 and isinstance(evidence[0],dict):
                evidence=evidence[0]
                record("fact_evidence_single_list_to_object")
            if "evidence" in item:
                item["evidence"]=project(evidence,EVIDENCE_FIELDS)
    return output,changes


def schema_diagnostic(error):
    """Never stringify ValidationError: its inputs, context and unknown keys can be PII."""
    labels={"missing":"缺少必填字段", "string_type":"应为文本", "list_type":"应为数组",
            "model_type":"应为对象", "model_attributes_type":"应为对象", "literal_error":"判定或字段名不在允许范围",
            "string_too_long":"文本超过长度限制", "string_too_short":"文本为空或过短",
            "too_long":"数组或文本超过长度限制", "extra_forbidden":"包含契约外字段"}
    errors=error.errors(include_input=False,include_context=False,include_url=False)
    details=[]
    for entry in errors[:6]:
        path=""
        for part in entry.get("loc",())[:8]:
            if isinstance(part,int):
                path+=f"[{part+1}]"
            else:
                path+=("." if path else "")+(part if part in SAFE_PATH_FIELDS else "未知字段")
        details.append(f"{path or '根对象'}：{labels.get(entry['type'],'字段格式不合法')}")
    suffix=f"；另有{len(errors)-6}处格式问题" if len(errors)>6 else ""
    return "模型输出结构不符（数组序号从1开始）："+"；".join(details)+suffix+"。未生成评分；诊断不含模型原文或字段取值。"


def parse_model_result(raw):
    normalized,changes=normalize_model_output(raw)
    try:
        return ModelResult.model_validate(normalized),changes
    except ValidationError as exc:
        raise BusinessError("MODEL_SCHEMA",schema_diagnostic(exc)) from exc


def output_schema():
    # Use the same schema as validation, including constraints and forbidden extras.
    # Sending it in the prompt keeps json_object-only providers compatible.
    return ModelResult.model_json_schema()


def format_adjustment_message(changes):
    return "已兼容模型输出格式："+"；".join(f"{label} {changes[key]} 处" for key,label in FORMAT_LABELS.items() if changes.get(key))+"。事实、引文和评分条目仍须校验；模型自报分数不参与计算。"
