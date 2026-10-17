"""CMM chat interface. Existing API and attachment transport are unchanged."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

import streamlit as st

from upload_support import SignedFileClient, InputError, UploadError, conversation_body, prepare_table, sheet_names


def setting(name):
    value = os.getenv(name)
    if value is None:
        try:
            value = st.secrets.get(name, "")
        except FileNotFoundError:
            value = ""
    return str(value).strip()


def show_answer(answer):
    if "未获得原始附件" in answer:
        st.warning("附件尚未进入计算环境，本次 CMM 风险计算未执行。您仍可继续进行健康咨询。", icon="📎")
        with st.expander("查看本次详细回复"):
            st.markdown(answer)
    else:
        st.markdown(answer)


st.set_page_config(page_title="筑梦健康毕节 · CMM个人健康助手", page_icon="🌿", layout="wide", initial_sidebar_state="auto")
st.markdown("""
<style>
:root { --cmm-teal:#0f766e; --cmm-ink:#173334; --cmm-muted:#597170; }
.stApp { background:#f5f8f8; color:var(--cmm-ink); }
html, body, .stApp, .stApp p, .stApp h1, .stApp label, .stApp button, .stApp textarea, .stApp li {
    font-family:"Microsoft YaHei","PingFang SC",sans-serif; }
[data-testid="stHeader"] { background:rgba(245,248,248,.92); }
[data-testid="stMainBlockContainer"] { max-width:1080px; padding-top:2.7rem; padding-bottom:2rem; }
[data-testid="stSidebar"] { background:#fff; border-right:1px solid #e1eae8; }
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
</style>
""", unsafe_allow_html=True)

defaults = {"messages": [], "session_id": None, "file_manifest": None, "file_ready": False,
    "file_sent": False, "rows": 0, "uploader_version": 0}
request_calculation = False
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

with st.sidebar:
    st.markdown('<div class="cmm-brand"><div class="cmm-logo">🌿</div><div><div class="cmm-brand-name">筑梦健康毕节</div><div class="cmm-brand-sub">CMM 个人健康助手</div></div></div>', unsafe_allow_html=True)
    if st.button("＋  开始新对话", use_container_width=True):
        version = st.session_state.uploader_version + 1
        for key, value in defaults.items():
            st.session_state[key] = value
        st.session_state.uploader_version = version
        st.rerun()
    st.divider()
    st.markdown('<div class="cmm-side-title">📎 上传体检数据</div><div class="cmm-side-note">选择数据表，上传后点击“开始 CMM 计算”。<br>支持 CSV / Excel（.xlsx） · 最大 5 MiB</div>', unsafe_allow_html=True)
    uploaded = st.file_uploader("选择体检数据表", type=["csv", "xlsx"], key=f"upload-{st.session_state.uploader_version}", label_visibility="collapsed")
    selected_sheet = None
    excel_ok = True
    if uploaded is not None and Path(uploaded.name).suffix.lower() == ".xlsx":
        try:
            selected_sheet = st.selectbox("📑 选择工作表", sheet_names(uploaded.getvalue()))
        except Exception:
            excel_ok = False
            st.error("Excel读取失败，请确认是有效的.xlsx文件或另存为CSV。")
    consent = st.checkbox("我同意将表格中用于 CMM 计算的28项数据发送至百炼处理。", key=f"consent-{st.session_state.uploader_version}")
    if st.button("↑  准备并上传", type="primary", use_container_width=True, disabled=uploaded is None or not consent or not excel_ok):
        st.session_state.file_manifest = None
        st.session_state.file_ready = False
        st.session_state.file_sent = False
        st.session_state.rows = 0
        try:
            with st.spinner("正在检查并上传…"):
                data, count = prepare_table(uploaded.getvalue(), Path(uploaded.name).suffix.lower(), selected_sheet)
                client = SignedFileClient({name: setting(name) for name in ("OSS_BUCKET", "OSS_ENDPOINT", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET")})
                manifest = client.upload(data, count)
                st.session_state.file_manifest = manifest
                st.session_state.rows = count
                st.session_state.file_ready = True
            st.success(f"已准备 {count} 条数据，并核对了文件完整性。请点击开始 CMM 计算。")
        except (InputError, UploadError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("准备文件未完成，未发送聊天请求。请检查表格格式和应用依赖。")
    request_calculation = st.button("▶  开始 CMM 计算", use_container_width=True,
        disabled=not st.session_state.file_ready or st.session_state.file_sent)
    if st.session_state.file_ready:
        state = f"📄 已上传 {st.session_state.rows} 条数据<br>计算状态：以实际工具执行结果为准"
    else:
        state = "💬 可直接进行健康咨询<br>需要计算时，再上传体检数据"
    st.markdown(f'<div class="cmm-file-state">{state}</div>', unsafe_allow_html=True)
    with st.expander("ⓘ 使用小提示"):
        st.write("健康咨询可以直接提问，无需先填问卷。风险计算需要完整的模型输入字段。")
        st.write("表格中的姓名、原始 ID 等额外列会在上传前移除；请自己保留原表以对应结果行号。")
        st.caption("私有文件链接有效15分钟。请先用测试数据验证计算和下载；上传成功不等于计算完成。")

st.markdown('''<div class="cmm-hero"><div><div class="cmm-eyebrow">🌱 个人健康 · 日常管理</div>
<h1>筑梦健康毕节</h1><p>让健康管理，从今天开始。<br>我是您的 CMM 智能助手，陪您一起看懂体检信息，把健康建议变成可以开始的小行动。</p></div>
<div class="cmm-hero-art">🌿</div></div>''', unsafe_allow_html=True)

suggestion = None
if not st.session_state.messages:
    columns = st.columns(3, gap="medium")
    cards = [
        ("💚", "个人健康建议", "围绕戒烟、饮酒、饮食与活动，了解适合自己的日常行动。"),
        ("📋", "体检信息解读", "解释常见指标的含义，梳理需要关注和进一步咨询的问题。"),
        ("📊", "CMM 风险计算", "上传符合要求的数据表，申请模型计算，并核对结果交付。"),
    ]
    for column, (icon, title, text) in zip(columns, cards):
        with column:
            st.markdown(f'<div class="cmm-card"><div class="cmm-card-icon">{icon}</div><div class="cmm-card-title">{title}</div><div class="cmm-card-text">{text}</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="cmm-section">您好，今天想了解什么？</div><div class="cmm-intro">可以直接输入问题，也可以从下面的话题开始。</div>', unsafe_allow_html=True)
    prompts = [
        ("🚭 戒烟与饮酒", "我想了解戒烟和避免饮酒方面的日常健康建议。请直接给出可以开始执行的行动。"),
        ("🥗 饮食与活动", "我想改善日常饮食和身体活动。请先给出适用的一般建议，不要生成问卷。"),
        ("🩺 体检指标", "体检报告中的血压、血糖和血脂分别表示什么？请用容易理解的语言说明。"),
    ]
    for column, (label, text) in zip(st.columns(3, gap="small"), prompts):
        with column:
            if st.button(label, use_container_width=True):
                suggestion = text

for message in st.session_state.messages:
    with st.chat_message(message["role"], avatar="🌿" if message["role"] == "assistant" else "👤"):
        if message["role"] == "assistant":
            show_answer(message["content"])
        else:
            st.markdown(message["content"])

st.markdown('<div class="cmm-footer">健康建议用于辅助日常管理，诊疗与用药请遵医嘱。</div>', unsafe_allow_html=True)
typed_prompt = st.chat_input("聊聊您的健康问题，例如：血压偏高，平时应该注意什么？")
prompt = "请对我刚上传的数据表执行CMM风险计算，并提供本次结果CSV的真实下载链接。" if request_calculation else (typed_prompt or suggestion)
if prompt:
    api_key = setting("DASHSCOPE_API_KEY")
    app_id = setting("BAILIAN_APP_ID")
    if not api_key or not app_id:
        st.error("请管理员检查现有百炼API Key和应用ID配置。")
        st.stop()
    manifest = st.session_state.file_manifest if request_calculation else None
    try:
        body = conversation_body(prompt, st.session_state.session_id, manifest)
    except InputError as exc:
        st.warning(str(exc))
        st.stop()
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    if setting("DASHSCOPE_WORKSPACE_ID"):
        headers["X-DashScope-WorkSpace"] = setting("DASHSCOPE_WORKSPACE_ID")
    request = urllib.request.Request("https://dashscope.aliyuncs.com/api/v1/apps/" + app_id + "/completion",
        data=json.dumps(body).encode(), headers=headers, method="POST")
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="👤"):
        st.markdown(prompt)
    with st.chat_message("assistant", avatar="🌿"):
        try:
            with st.spinner("正在为您整理回复…"):
                with urllib.request.urlopen(request, timeout=300) as response:
                    result = json.load(response)
            answer = result["output"].get("text", "服务返回了空回复。")
            st.session_state.session_id = result["output"].get("session_id")
            if manifest:
                st.session_state.file_sent = True
            show_answer(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})
        except urllib.error.HTTPError as exc:
            st.error(f"智能体调用失败（HTTP {exc.code}）。本次没有确认计算成功。")
        except Exception:
            st.error("请求未完成，请稍后重试。本次没有确认计算成功。")
