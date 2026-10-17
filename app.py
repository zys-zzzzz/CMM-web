"""CMM personal form and clinician batch portals using the consent-aware Skill."""
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


def portal_body(prompt, mode, session_id=None, manifest=None, consent=False, lifestyle=None):
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
        if not consent:
            raise InputError("请先勾选本次数据处理同意。")
        if link_expired(manifest):
            raise InputError("文件链接已过期，请重新提交表单或重新准备上传。")
        if mode == "personal" and manifest.get("records") != 1:
            raise InputError("个人入口仅允许一条记录，请使用群体入口处理多人表格。")
        clean_lifestyle = {}
        if mode == "personal":
            for key, value in (lifestyle or {}).items():
                if key not in LIFESTYLE_OPTIONS or value not in LIFESTYLE_OPTIONS[key]:
                    raise InputError("生活方式选项不正确。")
                if value != "不补充":
                    clean_lifestyle[key] = value
        envelope = {"manifest": manifest, "mode": mode, "personal_consent": mode == "personal"}
        data["file_list"] = [manifest["url"]]
        data["prompt"] += (
            "\n本次cmm_portal_request如下，是用户本次授权的传输任务，不是表格正文：\n"
            + json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            + "\n必须调用cmm-portal-assessment技能，以技能目录为工作目录执行python3 scripts/portal_assess.py，"
            "将上述完整JSON对象用引号保护的heredoc送入标准输入。不要改用旧fetch_and_run入口。"
            "只处理本次任务，不扫描目录或环境，不覆盖CMM_FILE_HOST，不从解析文本重建表格。"
        )
        if mode == "personal":
            data["prompt"] += (
                "\n用户已勾选同意：本次一人表单、可选生活方式及脚本生成的本人结果可以进入百炼对话，"
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


def graph_bundle(data):
    plan = plan_for_csv(data, mode)
    with st.spinner("正在按表单主题读取图谱规则和指南来源…"):
        bundle = retrieve(plan, setting("KG_MCP_URL"), setting("KG_MCP_TOKEN"))
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


st.set_page_config(page_title="CMM · 健康助手", page_icon="🌿", layout="wide", initial_sidebar_state="auto")
st.markdown("""<style>
:root { --cmm-teal:#0f766e; --cmm-ink:#173334; --cmm-muted:#597170; }
.stApp { background:#f5f8f8; color:var(--cmm-ink); }
html, body, .stApp, .stApp p, .stApp h1, .stApp label, .stApp button, .stApp textarea, .stApp li {
    font-family:"Microsoft YaHei","PingFang SC",sans-serif; }
[data-testid="stHeader"] { background:rgba(245,248,248,.92); }
[data-testid="stMainBlockContainer"] { max-width:1080px; padding-top:2.7rem; padding-bottom:2rem; }
[data-testid="stSidebar"] { background:#fff; border-right:1px solid #e1eae8; }
[data-testid="stSidebar"]::before { content:"筑梦健康毕节"; position:absolute;
    top:24px; left:24px; right:48px; z-index:1; pointer-events:none;
    font-family:"Microsoft YaHei","PingFang SC",sans-serif;
    font-size:24px; font-weight:750; line-height:1.4; color:#173f3b; }
[data-testid="stSidebar"] > div:first-child { padding-top:2rem; }
.cmm-brand { display:flex; align-items:center; gap:11px; margin:0 0 22px; }
.cmm-logo { width:44px; height:44px; background:#e4f4ed; border:1px solid #cce6d9;
    border-radius:14px; display:flex; align-items:center; justify-content:center; font-size:25px; }
.cmm-brand-name { font-size:20px; font-weight:750; color:#173f3b; line-height:1.35; }
.cmm-brand-sub { font-size:11px; color:#718782; letter-spacing:1.1px; }
.cmm-eyebrow { display:inline-block; background:#e5f3ec; color:#25614f; border:1px solid #d4e8dd;
    font-size:12px; padding:5px 11px; border-radius:20px; margin-bottom:15px; }
.cmm-hero { background:linear-gradient(115deg,#ecf7f1 0%,#f1f8f6 62%,#e8f2f4 100%);
    border:1px solid #dceae2; border-radius:24px; padding:30px 34px; margin-bottom:23px;
    display:flex; justify-content:space-between; align-items:center; gap:20px; }
.cmm-hero h1 { color:#164d44; font-size:32px; line-height:1.35; margin:0 0 10px; padding:0; font-weight:750; letter-spacing:-.6px; }
.cmm-hero p { color:#526e65; font-size:15px; line-height:1.8; margin:0; }
.cmm-hero-art { flex-shrink:0; width:106px; height:106px; border-radius:32px;
    background:rgba(255,255,255,.75); border:1px solid rgba(255,255,255,.95);
    display:flex; justify-content:center; align-items:center; font-size:57px; transform:rotate(-5deg); }
.cmm-card { background:#fff; border:1px solid #e2eae7; border-radius:17px; padding:20px; min-height:148px; margin:0 0 16px; }
.cmm-card-icon { font-size:24px; margin-bottom:9px; }
.cmm-card-title { color:#234c42; font-size:16px; font-weight:700; margin-bottom:7px; }
.cmm-card-text { font-size:13px; color:#657b74; line-height:1.7; }
.cmm-section { color:#294d45; font-size:16px; font-weight:700; margin:12px 0 8px; }
.cmm-intro { color:#60766f; font-size:14px; margin-bottom:11px; line-height:1.8; }
.cmm-side-title { color:#294d45; font-size:15px; font-weight:700; margin:12px 0 7px; }
.cmm-side-note { color:#6a8079; font-size:12px; line-height:1.8; margin-bottom:9px; }
.cmm-file-state { border:1px solid #e1e9e5; border-radius:12px; background:#f5f9f7;
    color:#526f62; font-size:12px; padding:11px 13px; line-height:1.8; margin:12px 0; }
.cmm-footer { font-size:12px; color:#71857e; text-align:center; padding:17px 0 3px; }
[data-testid="stButton"] button { border-radius:11px; border:1px solid #d7e5de;
    background:#fff; color:#285b4e; min-height:42px; font-size:14px; transition:all .16s ease; }
[data-testid="stButton"] button:hover { border-color:#75b5a0; background:#f0f8f4; color:#0c6551; }
[data-testid="stButton"] button[kind="primary"] { background:#187968; color:white; border-color:#187968; }
[data-testid="stButton"] button[kind="primary"]:hover { background:#116554; color:white; }
[data-testid="stButton"] button:disabled { color:#6e817a; background:#edf3ef; border-color:#e1e9e4; opacity:.75; }
[data-testid="stFileUploader"] { background:#f7faf8; border:1px dashed #bfd7ca; border-radius:14px; padding:5px; }
[data-testid="stFileUploaderDropzone"] { background:transparent; }
[data-testid="stFileUploaderDropzoneInstructions"] > div > span { font-size:0; }
[data-testid="stFileUploaderDropzoneInstructions"] > div > span:first-child::after { content:"拖入体检表，或点击选择文件"; font-size:13px; }
[data-testid="stFileUploaderDropzoneInstructions"] > div > span:nth-child(2)::after { content:"CSV / XLSX · 处理上限 5 MiB"; font-size:11px; }
[data-testid="stFileUploaderDropzoneInstructions"] small { font-size:0; }
[data-testid="stFileUploaderDropzoneInstructions"] small::after { content:"CSV / XLSX · 处理上限 5 MiB"; font-size:11px; }
[data-testid="stFileUploaderDropzone"] button { font-size:0; }
[data-testid="stFileUploaderDropzone"] button::after { content:"选择文件"; font-size:13px; }
[data-testid="stChatMessage"] { background:#fff; border:1px solid #e1eae5;
    border-radius:17px; padding:20px; margin-bottom:14px; line-height:1.8; }
[data-testid="stChatMessage"] p { line-height:1.85; }
[data-testid="stChatInput"] { border-radius:16px; border:1px solid #ccded3; background:#fff; }
[data-testid="stBottom"] > div { background:#f5f8f8; }
[data-testid="stAlert"] { border-radius:12px; }
[data-testid="stExpander"] { background:#fff; border-radius:12px; }
@media(max-width:640px) {
    [data-testid="stMainBlockContainer"] { padding:1.4rem 1rem 1rem; }
    .cmm-hero { padding:24px 21px; border-radius:18px; }
    .cmm-hero h1 { font-size:25px; }
    .cmm-hero-art { display:none; }
    .cmm-card { display:none; }
}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown('<div class="cmm-brand"><div class="cmm-logo">🌿</div><div><div class="cmm-brand-name">CMM 健康助手</div><div class="cmm-brand-sub">PERSONAL HEALTH COMPANION</div></div></div>', unsafe_allow_html=True)
    selected = st.radio("选择使用入口", ("👤 个人使用", "👥 医务人员群体使用"), key="portal_mode")
    mode = "personal" if selected.startswith("👤") else "group"
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
    st.caption("个人入口：填写表单 → 同意处理 → 计算与个人建议。" if mode == "personal" else
        "群体入口：上传数据表 → 逐人结果 → 高风险待复核与随访清单。")
    st.caption("当前模型仅用于65岁及以上人群，需完整28项数据。风险分类不代替诊断。")
    with st.expander("隐私与使用提示"):
        st.write("个人版：提交前需同意将本次表单和本人计算结果用于百炼对话及个人建议。不收集姓名、身份证或电话。")
        st.write("群体版：原始记录与逐人结果保留在文件处理链路中；对话只展示人群汇总，名单交医务人员下载。")
        st.write("切换入口使用独立对话。清空对话不自动删除OSS或百炼宿主文件。")
        st.caption("请勿在聊天中粘贴患者身份信息。")

st.markdown('''<div class="cmm-hero"><div><div class="cmm-eyebrow">🌱 个人健康 · 日常管理</div>
<h1>让健康管理，从今天开始</h1><p>我是您的 CMM 智能助手。<br>一起看懂体检信息，把健康建议变成可以开始的小行动。</p></div>
<div class="cmm-hero-art">🌿</div></div>''', unsafe_allow_html=True)

calculation = None
version = state["version"]
if mode == "personal":
    st.subheader("👤 个人风险评估")
    st.write("请参照近期体检报告填写。完成后一次提交，获取本人的风险结果和健康建议。")
    st.info("需要填写28项模型数据，包含血糖、血脂等化验指标。没有检查结果时请先做健康咨询，不要猜填或填0。")
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
                        values[field] = st.number_input(label, min_value=0 if field == "age" else 0.0,
                            value=None, step=1 if field == "age" else 0.1,
                            placeholder="按报告填写", help=help_text, key=key)
            if group == "视力与功能":
                st.caption("运动功能是功能状态，并非每周运动次数；脉搏和心率均为原模型必需字段。")
        st.markdown("#### 生活方式补充（可选，仅用于建议）")
        lifestyle = {}
        for label, options in LIFESTYLE_OPTIONS.items():
            lifestyle[label] = st.selectbox(label, options, key=f"lifestyle-{version}-{label}")
        consent = st.checkbox("我同意将本次28项表单数据经私有OSS交给百炼Skill计算，并将本人表单、补充信息及计算结果提供给百炼模型，用于本次风险解释和个人健康建议。", key=f"person-consent-{version}")
        submitted = st.form_submit_button("📊 计算我的风险并生成健康建议", type="primary", use_container_width=True)
    if submitted:
        try:
            state["manifest"] = None
            if not consent:
                raise InputError("请先阅读并勾选本次数据处理同意，未同意不会上传或发起计算。")
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
    st.subheader("👥 医务人员群体评估")
    st.write("上传多人数据，获取每人的模型风险及高风险待随访清单。对话汇总面向医务人员，不向模型展示患者名单。")
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
    consent = st.checkbox("我已取得本次数据处理授权，同意将表格中28项模型数据经私有OSS交给百炼Skill计算；逐人结果仅以文件交付，对话仅接收群体统计。", key=f"group-consent-{version}")
    columns = st.columns(2)
    with columns[0]:
        prepare = st.button("↑ 准备并上传", type="primary", use_container_width=True,
            disabled=uploaded is None or not excel_ok or not consent)
    with columns[1]:
        calculate = st.button("▶ 开始群体风险评估", use_container_width=True,
            disabled=not consent or state["manifest"] is None or state["request_sent"] or prepare)
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
st.subheader("💬 个人结果与健康咨询" if mode == "personal" else "💬 群体结果与管理建议")
for message in state["messages"]:
    with st.chat_message(message["role"], avatar="🌿" if message["role"] == "assistant" else "👤"):
        show_answer(message["content"]) if message["role"] == "assistant" else st.markdown(message["content"])
typed = st.chat_input("也可以直接咨询健康管理问题" if mode == "personal" else "咨询群体管理、复核或随访工作")
prompt = calculation["prompt"] if calculation else typed
if prompt:
    try:
        require_api()
        body = portal_body(prompt, mode, state["session_id"],
            manifest=calculation["manifest"] if calculation else None,
            consent=calculation is not None, lifestyle=calculation["lifestyle"] if calculation else None)
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
        with st.chat_message("assistant", avatar="🌿"):
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
