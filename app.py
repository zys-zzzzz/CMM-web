"""CMM personal form and clinician batch portals using the CMM assessment Skill."""
import csv
import io
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

import streamlit as st

from guideline_router import plan_for_csv, retrieve, evidence_prompt
from upload_support import SignedFileClient, InputError, UploadError, prepare_table, sheet_names, FIELDS, link_expired


# Portal functions bundled here so the existing site only needs app.py replaced.
FORM_GROUPS = (
    ("基本情况与疾病史", (
        ("age", "年龄（岁）", None),
        ("gender", "性别", ("男", "女")),
        ("smoke", "目前是否吸烟", ("否", "是")),
        ("hypertension_final", "是否已诊断高血压", ("否", "是")),
        ("diabetes_final", "是否已诊断糖尿病", ("否", "是")),
        ("dyslipidemia_final", "是否已诊断血脂异常", ("否", "是")),
    )),
    ("体格测量", (
        ("height_cm", "身高（cm）", None), ("weight_kg", "体重（kg）", None),
        ("waist_cm", "腰围（cm）", None), ("temperature", "体温（℃）", None),
        ("pulse", "脉搏（次/分钟）", None), ("heart_rate", "心率（次/分钟）", None),
        ("systolic", "收缩压（mmHg）", None), ("diastolic", "舒张压（mmHg）", None),
    )),
    ("视力与功能", (
        ("left_naked_eye", "左眼裸眼视力（小数制）", None),
        ("right_naked_eye", "右眼裸眼视力（小数制）", None),
        ("hear", "听力", ("正常", "异常")),
        ("exercise", "运动功能", ("正常", "异常")),
        ("heart_rhythm", "心律", ("规则", "不规则")),
    )),
    ("血常规与肾功能", (
        ("HGB", "血红蛋白（g/L）", None), ("PLT", "血小板（×10⁹/L）", None),
        ("Creatinine", "肌酐（μmol/L）", None), ("Urea", "尿素（mmol/L）", None),
    )),
    ("血糖与血脂", (
        ("TC", "总胆固醇（mmol/L）", None), ("LDL_C", "LDL-C（mmol/L）", None),
        ("HDL_C", "HDL-C（mmol/L）", None), ("glucose_mmolL", "血糖（mmol/L）", None),
        ("triglycerides_mmolL", "甘油三酯（mmol/L）", None),
    )),
)
# Engineering input ranges, not clinical reference intervals. Values can be entered manually.
SLIDER_SPECS = {
    "age": (65, 112, 70, 1),
    "height_cm": (100.0, 220.0, 165.0, 0.1),
    "weight_kg": (20.0, 200.0, 65.0, 0.1),
    "waist_cm": (40.0, 180.0, 85.0, 0.1),
    "temperature": (34.0, 42.0, 36.5, 0.1),
    "pulse": (30.0, 200.0, 70.0, 1.0),
    "heart_rate": (30.0, 200.0, 70.0, 1.0),
    "systolic": (60.0, 250.0, 130.0, 1.0),
    "diastolic": (30.0, 150.0, 80.0, 1.0),
    "left_naked_eye": (0.1, 2.0, 1.0, 0.1),
    "right_naked_eye": (0.1, 2.0, 1.0, 0.1),
    "HGB": (20.0, 250.0, 135.0, 1.0),
    "PLT": (10.0, 1000.0, 250.0, 1.0),
    "Creatinine": (10.0, 1500.0, 70.0, 1.0),
    "Urea": (0.1, 60.0, 5.0, 0.1),
    "TC": (0.1, 20.0, 4.5, 0.01),
    "LDL_C": (0.01, 15.0, 2.5, 0.01),
    "HDL_C": (0.01, 5.0, 1.2, 0.01),
    "glucose_mmolL": (0.1, 40.0, 5.5, 0.01),
    "triglycerides_mmolL": (0.1, 30.0, 1.5, 0.01),
}

LIFESTYLE_OPTIONS = {
    "饮酒情况": ("不补充", "不饮酒", "饮酒"),
    "日常身体活动": ("不补充", "较少活动", "有规律活动"),
}


def personal_csv(values):
    if set(values) != set(FIELDS):
        raise InputError("表单字段不完整，请刷新页面后重新填写。")
    missing = [field for field in FIELDS if values[field] is None]
    if missing:
        labels = {field: label for _, fields in FORM_GROUPS for field, label, _ in fields}
        raise InputError("请补充：" + "、".join(labels[field] for field in missing))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerow(values)
    return prepare_table(output.getvalue().encode("utf-8"), ".csv")


def blank_template():
    return (",".join(FIELDS) + "\n").encode("utf-8-sig")


def portal_body(prompt, mode, session_id=None, manifest=None, lifestyle=None):
    if mode not in ("personal", "group"):
        raise InputError("使用入口不正确。")
    role = (
        "当前为个人使用入口。面向本人回答。普通咨询先直接给出健康建议，不强制填写表单。"
        if mode == "personal" else
        "当前为医务人员群体使用入口。面向医务人员回答，提供人群管理、复核和随访工作建议。"
    )
    data = {"prompt": role + "\n" + prompt}
    if session_id:
        data["session_id"] = session_id
    if manifest is not None:
        if link_expired(manifest):
            raise InputError("文件链接已过期，请重新提交表单或重新准备上传。")
        if mode == "personal" and manifest.get("records") != 1:
            raise InputError("个人入口仅允许一条记录，请使用群体入口处理多人表格。")
        clean_lifestyle = {}
        if mode == "personal":
            for key, value in (lifestyle or {}).items():
                if value is None:
                    continue
                if key not in LIFESTYLE_OPTIONS or value not in LIFESTYLE_OPTIONS[key]:
                    raise InputError("生活方式选项不正确。")
                if value != "不补充":
                    clean_lifestyle[key] = value
        envelope = {"manifest": manifest, "mode": mode, "personal_consent": mode == "personal"}
        data["file_list"] = [manifest["url"]]
        data["prompt"] += (
            "\n以下 cmm_portal_request 由用户主动提交评估后生成，包含本次文件传输清单：\n"
            + json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            + "\n必须调用cmm-portal-assessment技能，以技能目录为工作目录执行python3 scripts/portal_assess.py，"
            "将上述完整JSON对象用引号保护的heredoc送入标准输入。不要改用旧fetch_and_run入口。"
            "只处理本次任务，不扫描目录或环境，不覆盖CMM_FILE_HOST，不从解析文本重建表格。"
        )
        if mode == "personal":
            data["prompt"] += (
                "\n个人入口已提交本次评估。一人表单、可选生活方式及本人结果用于百炼对话，"
                "用于本次风险解释和个人建议。等脚本completed后使用personal_result和personal_facts，"
                "先展示真实概率、0.435阈值分类及范围提醒，再给本人可执行的建议。"
                "未经填写的信息不得推断。不得把个人值、风险结果或链接发送给Neo4j MCP。"
                "图谱仅查询通用主题和规则编号，再结合其适用条件在当前对话组织建议。"
                "可选生活方式只影响建议，不得加入模型特征或改变概率。"
                "\n用户补充的生活方式：" + json.dumps(clean_lifestyle, ensure_ascii=False)
            )
        else:
            data["prompt"] += (
                "\n群体入口不得读取、打印或向模型传送逐人输入、逐人概率或高风险名单。"
                "只使用脚本实际返回的summary报告人数、高风险人数与比例和范围警告人数；"
                "导出本次result.csv及followup.csv，后者是按模型阈值筛出的待复核清单，不是诊断或完成随访。"
                "向医务人员建议复核输入和适用范围、对高风险记录进行联系评估、建立随访计划、记录处置和复评。"
                "随访频率按已确诊疾病、现状及实际指南由医务人员决定，不能从模型分类编造固定频率或处方。"
                "低风险不等于无需健康管理。不得将本批统计、文件或任务信息传给MCP。"
            )
        data["prompt"] += (
            "\n只有本次脚本确实返回completed才声明完成。使用宿主download_file导出脚本返回的文件，"
            "提供真实可下载链接，不能伪造。计算失败只报告固定错误码，不给出猜测结果。"
        )
    return {"input": data, "parameters": {}}


def setting(name):
    value = os.getenv(name)
    if value is None:
        try:
            value = st.secrets.get(name, "")
        except FileNotFoundError:
            value = ""
    return str(value).strip()


def require_api():
    if not setting("DASHSCOPE_API_KEY") or not setting("BAILIAN_APP_ID"):
        raise InputError("请管理员配置百炼API Key和App ID后再提交。")


def upload(data, records):
    keys = ("OSS_BUCKET", "OSS_ENDPOINT", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET")
    return SignedFileClient({key: setting(key) for key in keys}).upload(data, records)


def guideline_mcp_settings():
    url, token = setting("KG_MCP_URL"), setting("KG_MCP_TOKEN")
    if url or token:
        return url, token
    try:
        return tuple(str(st.secrets.get(key, "")).strip() for key in ("url", "token"))
    except FileNotFoundError:
        return "", ""


def graph_bundle(data):
    plan = plan_for_csv(data, mode)
    with st.spinner("正在按表单主题读取图谱规则和指南来源…"):
        bundle = retrieve(plan, *guideline_mcp_settings())
    return plan, bundle


def show_knowledge(bundle):
    if not bundle:
        return
    if bundle["status"] == "complete":
        st.success("已实际检索并核对本次候选规则的来源字段；是否适用仍需核对条件。")
    else:
        st.warning("本次指南来源未完整取得。已缺失的主题不能当作指南支持的个人建议。风险计算可独立进行。")
    with st.expander("查看本次指南检索记录"):
        st.write("检索主题：" + "、".join(bundle["topics"]))
        for row in bundle["evidence"]:
            st.write(row["rule_id"] + " · " + row["source"] + " · " + row["version"] + " · " + row["locator"])
            st.write("候选管理规则：" + row["recommendation"])
            st.write("适用人群：" + row["population"] + "；条件：" + row["conditions"])
            if row["conditional"]:
                st.caption("仅供核实条件后参考，未判定适用于本人。")
        for missing in bundle["missing"]:
            st.caption("未取得：" + missing.get("rule_id", missing.get("topic", "连接或接口")) + " · " + missing["code"])


def show_answer(answer):
    if "未获得原始附件" in answer or "附件尚未进入计算环境" in answer:
        st.warning("本次没有确认计算完成，请检查新Skill是否已挂载和发布。", icon="📎")
        with st.expander("查看详细回复"):
            st.markdown(answer)
    else:
        st.markdown(answer)


st.set_page_config(page_title="CMM-SCREEN", page_icon="📋", layout="wide", initial_sidebar_state="auto")
st.markdown("""<style>
:root {
    --cmm-bg:#f4f5fc;
    --cmm-ink:#262c4e;
    --cmm-muted:#515976;
    --cmm-border:#dce0f2;
    --cmm-primary:#4e54b5;
}
.stApp { background:var(--cmm-bg); color:var(--cmm-ink); }
.stApp :is(h1,h2,h3,h4,p,label,button,textarea) {
    font-family:"Microsoft YaHei","PingFang SC",sans-serif;
}
[data-testid="stHeader"] { background:rgba(244,245,252,.96); }
[data-testid="stMainBlockContainer"], .block-container {
    max-width:1160px; padding-top:5.5rem; padding-bottom:2rem;
}
[data-testid="stSidebar"] { background:#fff; border-right:1px solid var(--cmm-border); }
.cmm-brand {
    display:flex; align-items:center; gap:11px; margin-bottom:14px;
    color:var(--cmm-ink); font-size:17px; font-weight:700;
}
.cmm-brand-caption {
    color:var(--cmm-muted); font-size:12px; font-weight:400;
    line-height:1.55; margin-top:4px;
}
.cmm-logo {
    display:grid; place-items:center; width:42px; height:42px; flex:none;
    border-radius:9px; background:#444aa1; color:#fff; font-size:11px; letter-spacing:.04em;
}
.cmm-hero {
    display:flex; align-items:center; justify-content:space-between; gap:24px;
    background:linear-gradient(115deg,#3444a4 0%,#6150ad 68%,#8170c1 100%);
    border-radius:26px; padding:28px 36px; margin-bottom:16px;
    box-shadow:0 12px 34px #3444a41a;
}
.cmm-hero h1 { margin:0 0 10px; padding:0; color:#fff; font-size:clamp(1.6rem,3vw,2.25rem); line-height:1.35; }
.cmm-hero p { margin:0; color:#eef0ff; font-size:16px; line-height:1.7; }
.cmm-hero-art { width:190px; height:152px; flex:none; }
.cmm-entry {
    display:flex; flex-direction:column; gap:12px; background:#fff; padding:26px;
    border:1px solid #dce0f4; border-radius:21px; min-height:190px; margin-bottom:14px;
    box-shadow:0 6px 22px #3346a809;
}
.cmm-entry-icon {
    display:grid; place-items:center; width:38px; height:38px; border-radius:10px;
    background:#eeeffb; color:#5258b1; font-size:23px;
}
.cmm-entry h3 { margin:0; padding:0; color:#2a305e; font-size:1.25rem; }
.cmm-entry p { margin:0; color:#515976; font-size:15px; line-height:1.8; }
[data-testid="stForm"] {
    background:#fff; border:1px solid var(--cmm-border);
    border-radius:14px; padding:clamp(12px,3vw,24px);
}
[data-testid="stForm"] h4 {
    color:#373d80; border-bottom:1px solid #e5e7f5; margin-bottom:8px; padding-bottom:8px;
}
[data-testid="stCaptionContainer"] p { color:var(--cmm-muted); line-height:1.6; }
:is([data-testid="stButton"],[data-testid="stFormSubmitButton"],[data-testid="stDownloadButton"]) button {
    border:1px solid var(--cmm-border); border-radius:8px; min-height:42px;
    background:#fff; color:#343a6e;
}
:is([data-testid="stButton"],[data-testid="stFormSubmitButton"],[data-testid="stDownloadButton"]) button:hover:not(:disabled) {
    background:#eff0fc; border-color:#858ac8; color:#383e93;
}
:is([data-testid="stButton"],[data-testid="stFormSubmitButton"]) button[kind="primary"]:not(:disabled) {
    background:var(--cmm-primary); border-color:var(--cmm-primary); color:#fff;
}
:is([data-testid="stButton"],[data-testid="stFormSubmitButton"]) button[kind="primary"]:hover:not(:disabled) {
    background:#3f459d; border-color:#3f459d; color:#fff;
}
[data-testid="stFileUploaderDropzone"] {
    background:#fff; border:1px dashed #a0a6d1; border-radius:12px;
}
[data-testid="stFileUploaderDropzoneInstructions"] > div > span:first-child { font-size:0; }
[data-testid="stFileUploaderDropzoneInstructions"] > div > span:first-child::after {
    content:"拖入体检表，或点击选择文件"; font-size:14px;
}
[data-testid="stFileUploaderDropzoneInstructions"] small { font-size:0; }
[data-testid="stFileUploaderDropzoneInstructions"] small::after { content:"CSV / XLSX"; font-size:12px; }
[data-testid="stFileUploaderDropzone"] button { font-size:0; }
[data-testid="stFileUploaderDropzone"] button::after { content:"选择文件"; font-size:14px; }
[data-testid="stChatMessage"] {
    background:#fff; border:1px solid var(--cmm-border); border-radius:12px;
}
[data-testid="stChatMessage"] p { line-height:1.75; }
[data-testid="stChatInput"] { border:1px solid #b9bee1; border-radius:12px; background:#fff; }
[data-testid="stBottom"] > div { background:var(--cmm-bg); }
[data-testid="stExpander"] { background:#fff; border-radius:10px; }
@media(max-width:640px) {
    [data-testid="stMainBlockContainer"], .block-container { padding:3.75rem 1rem 1rem; }
    .cmm-hero { padding:21px 18px; border-radius:12px; }
    .cmm-hero h1 { font-size:24px; }
    .cmm-hero-art { display:none; }
    .cmm-entry { min-height:0; padding:20px; }
}
</style>""", unsafe_allow_html=True)

selected = st.session_state.get("assessment_view", "工作台")
mode = {"工作台": "home", "个人评估": "personal", "群体评估": "group"}[selected]

with st.sidebar:
    st.markdown('<div class="cmm-brand"><span class="cmm-logo" aria-hidden="true">CMM</span><div><div>CMM-SCREEN</div><div class="cmm-brand-caption">老年心血管代谢性共病风险分层与健康管理助手</div></div></div>', unsafe_allow_html=True)
    if mode != "home":
        st.button(
            "返回工作台", use_container_width=True,
            on_click=st.session_state.__setitem__, args=("assessment_view", "工作台"),
        )
        state_key = "portal_" + mode
        if state_key not in st.session_state:
            st.session_state[state_key] = {"messages": [], "session_id": None, "manifest": None,
                "file_signature": None, "request_sent": False, "version": 0, "knowledge": None}
        state = st.session_state[state_key]
        if st.button("＋  开始新对话并清空本入口", use_container_width=True):
            state.update(messages=[], session_id=None, manifest=None, file_signature=None,
                request_sent=False, knowledge=None, version=state["version"] + 1)
            st.rerun()
    st.divider()
    st.caption("当前模型仅用于65岁及以上人群，需完整28项数据。风险分类不代替诊断。")
    with st.expander("隐私与使用提示"):
        st.write("个人版：提交评估后，本次表单及计算结果用于百炼风险解释和个人建议。请勿在表单或对话中填写身份信息。")
        st.write("群体版：原始记录与逐人结果保留在文件处理链路中；对话只展示人群汇总，名单交医务人员下载。")
        st.write("切换入口使用独立对话。清空对话不自动删除OSS或百炼宿主文件。")
        st.caption("请勿在聊天中粘贴患者身份信息。")

title, description = {
    "home": ("CMM-SCREEN", "老年心血管代谢性共病风险分层与健康管理助手"),
    "personal": ("个人风险评估", "填写体检指标，查看个人风险分层与健康管理建议。"),
    "group": ("群体风险评估", "上传体检表，生成逐人评估结果及高风险待复核清单。"),
}[mode]
hero_art = """<svg class="cmm-hero-art" viewBox="0 0 240 170" aria-hidden="true">
<circle cx="125" cy="85" r="74" fill="#ffffff" fill-opacity=".13"/>
<rect x="75" y="29" width="103" height="125" rx="19" fill="#f7f8ff"/>
<rect x="98" y="18" width="58" height="23" rx="10" fill="#b9c4ff"/>
<text x="126" y="82" text-anchor="middle" fill="#4a48a8" font-size="35" font-weight="700">65+</text>
<path d="M86 108h24l9-18 13 35 10-17h23" stroke="#6875d9" stroke-width="5" fill="none" stroke-linecap="round"/>
<circle cx="188" cy="125" r="27" fill="#ffb9bd"/><path d="M175 125h26M188 112v26" stroke="#fff" stroke-width="5" stroke-linecap="round"/>
</svg>"""
st.markdown(
    f'<div class="cmm-hero"><div><h1>{title}</h1><p>{description}</p></div>{hero_art}</div>',
    unsafe_allow_html=True,
)

if mode == "home":
    st.subheader("选择服务入口")
    cards = (
        ("个人评估", "♡", "参照个人体检报告填写指标，查看风险分层和健康建议。"),
        ("群体评估", "▦", "供医务人员批量上传体检表，下载逐人结果和待复核清单。"),
    )
    for column, (entry, icon, detail) in zip(st.columns(2, gap="large"), cards):
        with column:
            st.markdown(
                f'<div class="cmm-entry"><span class="cmm-entry-icon" aria-hidden="true">{icon}</span>'
                f'<h3>{entry}</h3><p>{detail}</p></div>',
                unsafe_allow_html=True,
            )
            st.button(
                f"进入{entry}", type="primary", use_container_width=True,
                on_click=st.session_state.__setitem__, args=("assessment_view", entry),
            )
    st.stop()

calculation = None
version = state["version"]
if mode == "personal":
    st.write("请参照近期体检报告填写。完成后一次提交，获取本人的风险结果和健康建议。")
    st.info("拖动滑块填写数值，性别与疾病史点击选择。起始值只是操作示例，不是您的检查结果；请按报告逐项核对，没有结果时不要猜填。")
    st.caption("滑块范围仅用于录入，不是医学正常范围。真实数值超出范围或需要更高精度时，请切换精确输入，不要硬填到端点。")
    precise_input = st.toggle("切换为精确手动输入", value=False, key=f"precise-input-{version}")
    with st.form(f"personal-form-{version}"):
        values = {}
        for group, fields in FORM_GROUPS:
            st.markdown("#### " + group)
            columns = st.columns(2)
            for index, (field, label, options) in enumerate(fields):
                with columns[index % 2]:
                    key = f"person-{version}-{field}"
                    if options is not None:
                        values[field] = st.selectbox(label, (0, 1), index=None,
                            format_func=lambda value, choices=options: choices[value],
                            placeholder="请选择；不清楚时先核实", key=key)
                    else:
                        help_text = "运动功能编码并非运动频次。" if field == "exercise" else None
                        if field in ("left_naked_eye", "right_naked_eye"):
                            help_text = "填写小数视力，如1.0，不能直接填4.9等对数视力。"
                        if precise_input:
                            values[field] = st.number_input(label, min_value=0 if field == "age" else 0.0,
                                value=None, step=1 if field == "age" else 0.01,
                                placeholder="按报告填写", help=help_text, key=key + "-exact")
                        else:
                            low, high, initial, step = SLIDER_SPECS[field]
                            values[field] = st.slider(label, min_value=low, max_value=high,
                                value=initial, step=step, format="%d" if field == "age" else "%.2f",
                                help=help_text, key=key + "-slider")
            if group == "视力与功能":
                st.caption("运动功能是功能状态，并非每周运动次数；脉搏和心率均为原模型必需字段。")
        st.markdown("#### 生活方式")
        lifestyle = {}
        for label, options in LIFESTYLE_OPTIONS.items():
            lifestyle[label] = st.selectbox(label, options[1:], index=None, placeholder="请选择",
                                            key=f"lifestyle-{version}-{label}")
        confirmed_values = st.checkbox("我已按实际报告逐项核对所有数值，包括没有拖动过的滑块起始值；没有用示例值代替未知检查结果。", key=f"person-values-confirmed-{version}")
        st.caption("提交后，28 项数据经私有 OSS 交由百炼 Skill 计算。百炼将使用本次表单、补充信息及计算结果生成风险解释和建议。")
        submitted = st.form_submit_button("📊 计算我的风险并生成健康建议", type="primary", use_container_width=True)
    if submitted:
        try:
            state["manifest"] = None
            if not confirmed_values:
                raise InputError("请先按实际报告核对所有数值并勾选确认，不能直接用滑块示例值计算。")
            require_api()
            data, records = personal_csv(values)
            plan, bundle = graph_bundle(data)
            with st.spinner("正在校验表单并准备本次计算…"):
                manifest = upload(data, records)
            calculation = {"manifest": manifest, "lifestyle": lifestyle, "plan": plan, "knowledge": bundle,
                "prompt": "请计算我的CMM风险，展示本人真实计算结果，随后结合本次信息给出个人健康建议和可用来源。"}
        except (InputError, UploadError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("表单准备未完成，本次没有确认计算成功。请检查配置后重新提交。")
else:
    st.download_button("⬇ 下载28字段空白CSV模板", blank_template(), "CMM输入模板.csv", "text/csv", key="group-template")
    uploaded = st.file_uploader("上传体检数据表（CSV / XLSX，最大5MiB、10000条）", type=["csv", "xlsx"], key=f"group-upload-{version}")
    sheet = None
    excel_ok = True
    if uploaded is not None and Path(uploaded.name).suffix.lower() == ".xlsx":
        try:
            sheet = st.selectbox("选择工作表", sheet_names(uploaded.getvalue()), key=f"group-sheet-{version}")
        except Exception:
            excel_ok = False
            st.error("Excel读取失败，请确认格式为.xlsx，或另存为CSV。")
    signature = (hashlib.sha256(uploaded.getvalue()).hexdigest(), sheet) if uploaded is not None else None
    if signature != state["file_signature"]:
        state.update(manifest=None, file_signature=signature, request_sent=False)
    st.caption("请上传已获处理授权的数据。点击准备并上传后，所需字段经私有 OSS 交由百炼 Skill 计算。逐人结果通过文件交付，对话使用群体统计。")
    columns = st.columns(2)
    with columns[0]:
        prepare = st.button("↑ 准备并上传", type="primary", use_container_width=True,
            disabled=uploaded is None or not excel_ok)
    with columns[1]:
        calculate = st.button("▶ 开始群体风险评估", use_container_width=True,
            disabled=state["manifest"] is None or state["request_sent"] or prepare)
    if prepare:
        state.update(manifest=None, request_sent=False)
        try:
            require_api()
            data, records = prepare_table(uploaded.getvalue(), Path(uploaded.name).suffix.lower(), sheet)
            with st.spinner("正在校验并上传…"):
                state["manifest"] = upload(data, records)
            st.success(f"已准备{records}条数据并核对文件完整性。请点击开始群体风险评估。")
            st.rerun()
        except (InputError, UploadError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("文件准备失败，请检查输入和配置。本次未确认计算成功。")
    if state["manifest"]:
        st.caption(f"当前已准备{state['manifest']['records']}条；链接15分钟有效。计算与下载以实际工具返回为准。")
    if calculate:
        data, _ = prepare_table(uploaded.getvalue(), Path(uploaded.name).suffix.lower(), sheet)
        plan, bundle = graph_bundle(data)
        calculation = {"manifest": state["manifest"], "lifestyle": None, "plan": plan, "knowledge": bundle,
            "prompt": "请对本次数据执行群体CMM评估，报告实际统计，导出逐人结果和高风险待随访清单，并给医务人员管理建议。"}
    st.caption("输入中的姓名、ID等额外列会在上传前去除。医务人员需自己保存原表，按数据行号对应结果。清单不是已完成随访。")

if calculation:
    state.update(messages=[], session_id=None, knowledge=calculation["knowledge"])
show_knowledge(state.get("knowledge"))
st.divider()
st.subheader("个人结果与健康咨询" if mode == "personal" else "群体结果与管理建议")
for message in state["messages"]:
    with st.chat_message(message["role"], avatar="📋" if message["role"] == "assistant" else "👤"):
        show_answer(message["content"]) if message["role"] == "assistant" else st.markdown(message["content"])
typed = st.chat_input("也可以直接咨询健康管理问题" if mode == "personal" else "咨询群体管理、复核或随访工作")
prompt = calculation["prompt"] if calculation else typed
if prompt:
    try:
        require_api()
        body = portal_body(prompt, mode, state["session_id"],
            manifest=calculation["manifest"] if calculation else None,
            lifestyle=calculation["lifestyle"] if calculation else None)
        if calculation:
            body["input"]["prompt"] += evidence_prompt(calculation["plan"], calculation["knowledge"], mode)
        # New calculations start independent sessions, preventing previous-person results from mixing.
        if calculation:
            body["input"].pop("session_id", None)
            state.update(messages=[], session_id=None)
        headers = {"Authorization": "Bearer " + setting("DASHSCOPE_API_KEY"), "Content-Type": "application/json"}
        if setting("DASHSCOPE_WORKSPACE_ID"):
            headers["X-DashScope-WorkSpace"] = setting("DASHSCOPE_WORKSPACE_ID")
        req = urllib.request.Request("https://dashscope.aliyuncs.com/api/v1/apps/" + setting("BAILIAN_APP_ID") + "/completion",
            data=json.dumps(body).encode(), headers=headers, method="POST")
        state["messages"].append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar="👤"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar="📋"):
            with st.spinner("正在计算并整理建议，请勿重复提交…"):
                with urllib.request.urlopen(req, timeout=300) as response:
                    result = json.load(response)
            answer = result["output"].get("text") or "服务未返回有效回复，本次没有确认计算完成。"
            state["session_id"] = result["output"].get("session_id")
            state["request_sent"] = bool(calculation)
            state["messages"].append({"role": "assistant", "content": answer})
            show_answer(answer)
    except (InputError, UploadError) as exc:
        st.error(str(exc))
    except urllib.error.HTTPError as exc:
        st.error(f"百炼调用失败（HTTP {exc.code}），本次没有确认计算成功。")
    except Exception:
        st.error("请求未完成，本次没有确认计算成功。请稍后重新提交；不要依据不完整回复作出判断。")
st.caption("健康管理建议用于辅助日常管理。诊断、药物调整与随访安排由医务人员结合实际情况决定。")
