"""Isolated upload candidate; preserves the existing DashScope chat endpoint."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

import streamlit as st

from upload_support import FileClient, InputError, UploadError, conversation_body, prepare_table, sheet_names


def setting(name):
    # Keep the working environment-variable path; Secrets is a fallback.
    value = os.getenv(name)
    if value is None:
        try:
            value = st.secrets.get(name, "")
        except FileNotFoundError:
            value = ""
    return str(value).strip()


st.set_page_config(page_title="CMM智能助手", page_icon="🏥")
st.title("CMM智能助手")
defaults = {"messages": [], "session_id": None, "file_id": None, "file_ready": False,
    "file_sent": False, "rows": 0, "uploader_version": 0}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value

with st.sidebar:
    if st.button("开始新对话"):
        version = st.session_state.uploader_version + 1
        for key, value in defaults.items():
            st.session_state[key] = value
        st.session_state.uploader_version = version
        st.rerun()
    st.subheader("上传体检数据表")
    st.caption("CSV或Excel（.xlsx），最多5MiB。先用测试表验证上传与计算。")
    uploaded = st.file_uploader("选择数据表", type=["csv", "xlsx"], key=f"upload-{st.session_state.uploader_version}")
    selected_sheet = None
    excel_ok = True
    if uploaded is not None and Path(uploaded.name).suffix.lower() == ".xlsx":
        try:
            selected_sheet = st.selectbox("选择工作表", sheet_names(uploaded.getvalue()))
        except Exception:
            excel_ok = False
            st.error("Excel读取失败，请确认是有效的.xlsx文件或另存为CSV。")
    consent = st.checkbox("我同意将这份表格中用于CMM计算的28项数据发送到百炼处理。", key=f"consent-{st.session_state.uploader_version}")
    if st.button("准备并上传", disabled=uploaded is None or not consent or not excel_ok):
        # Clear any prior attachment first: failed replacement must not send stale data.
        st.session_state.file_id = None
        st.session_state.file_ready = False
        st.session_state.file_sent = False
        st.session_state.rows = 0
        try:
            api_key = setting("DASHSCOPE_API_KEY")
            workspace = setting("BAILIAN_UPLOAD_WORKSPACE_ID") or setting("DASHSCOPE_WORKSPACE_ID")
            if not api_key or not workspace:
                raise InputError("请管理员在Secrets填写BAILIAN_UPLOAD_WORKSPACE_ID，保留现有百炼API Key。")
            with st.spinner("检查并上传中…"):
                data, count = prepare_table(uploaded.getvalue(), Path(uploaded.name).suffix.lower(), selected_sheet)
                client = FileClient(api_key, workspace)
                file_id = client.upload(data)
                st.session_state.file_id = file_id
                st.session_state.rows = count
                st.session_state.file_ready = client.status(file_id)
            if st.session_state.file_ready:
                st.success(f"百炼已接收{count}条数据。请在聊天框发送评估要求。")
            else:
                st.info("文件已上传，仍在处理中，请稍后点击检查文件状态。")
        except (InputError, UploadError) as exc:
            st.error(str(exc))
        except Exception:
            st.error("准备文件未完成，未发送聊天请求。请检查表格格式和应用依赖。")
    if st.session_state.file_id and not st.session_state.file_ready:
        if st.button("检查文件状态"):
            try:
                client = FileClient(setting("DASHSCOPE_API_KEY"), setting("BAILIAN_UPLOAD_WORKSPACE_ID") or setting("DASHSCOPE_WORKSPACE_ID"))
                st.session_state.file_ready = client.status(st.session_state.file_id)
                if st.session_state.file_ready:
                    st.success("文件已就绪，请发送评估要求。")
                else:
                    st.info("文件仍在处理中，请稍后再检查。")
            except (InputError, UploadError) as exc:
                st.error(str(exc))

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("输入问题或CMM评估要求"):
    if st.session_state.file_id and not st.session_state.file_ready:
        st.warning("请先等待附件就绪，或开始新对话进行纯文字咨询。")
        st.stop()
    api_key = setting("DASHSCOPE_API_KEY")
    app_id = setting("BAILIAN_APP_ID")
    if not api_key or not app_id:
        st.error("请管理员检查现有百炼API Key和应用ID配置。")
        st.stop()
    file_id = st.session_state.file_id if not st.session_state.file_sent else None
    body = conversation_body(prompt, st.session_state.session_id, file_id)
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    # Upload workspace is separate; do not add a new header to working chat requests.
    if setting("DASHSCOPE_WORKSPACE_ID"):
        headers["X-DashScope-WorkSpace"] = setting("DASHSCOPE_WORKSPACE_ID")
    request = urllib.request.Request("https://dashscope.aliyuncs.com/api/v1/apps/" + app_id + "/completion",
        data=json.dumps(body).encode(), headers=headers, method="POST")
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            with st.spinner("正在处理…"):
                with urllib.request.urlopen(request, timeout=300) as response:
                    result = json.load(response)
            answer = result["output"].get("text", "服务返回了空回复。")
            st.session_state.session_id = result["output"].get("session_id")
            st.session_state.file_sent = True
            st.markdown(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})
        except urllib.error.HTTPError as exc:
            st.error(f"智能体调用失败（HTTP {exc.code}）。本次没有确认计算成功。")
        except Exception:
            st.error("请求未完成，请稍后重试。本次没有确认计算成功。")
