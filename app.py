"""Server-side Bailian chat UI; put behind authenticated HTTPS reverse proxy."""
import json
import os
import urllib.error
import urllib.request
import uuid

import streamlit as st

st.set_page_config(page_title="CMM智能助手", page_icon="🏥")
st.title("CMM智能助手")
if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.session_id = None
    st.session_state.file_url = None
    st.session_state.file_sent = False

with st.sidebar:
    if st.button("开始新对话"):
        for key in ("messages", "session_id", "file_url", "file_sent"):
            st.session_state.pop(key, None)
        st.rerun()
    if os.getenv("OSS_BUCKET"):
        uploaded = st.file_uploader("上传体检CSV", type=["csv"])
        if uploaded is not None and st.button("准备文件"):
            try:
                import oss2
                bucket = oss2.Bucket(oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]),
                    os.environ["OSS_ENDPOINT"], os.environ["OSS_BUCKET"])
                object_key = "cmm-input/" + uuid.uuid4().hex + ".csv"
                bucket.put_object(object_key, uploaded.getvalue())
                st.session_state.file_url = bucket.sign_url("GET", object_key, 900)
                st.session_state.file_sent = False
                st.success("文件已准备，请发送评估要求。")
            except Exception:
                st.error("文件上传失败，请联系管理员检查存储配置。")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("输入问题或CMM评估要求"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    input_data = {"prompt": prompt}
    if st.session_state.session_id:
        input_data["session_id"] = st.session_state.session_id
    if st.session_state.file_url and not st.session_state.file_sent:
        input_data["file_list"] = [st.session_state.file_url]
    headers = {"Authorization": "Bearer " + os.environ["DASHSCOPE_API_KEY"], "Content-Type": "application/json"}
    if os.getenv("DASHSCOPE_WORKSPACE_ID"):
        headers["X-DashScope-WorkSpace"] = os.environ["DASHSCOPE_WORKSPACE_ID"]
    request = urllib.request.Request("https://dashscope.aliyuncs.com/api/v1/apps/" + os.environ["BAILIAN_APP_ID"] + "/completion",
        data=json.dumps({"input": input_data, "parameters": {}}).encode(), headers=headers, method="POST")
    with st.chat_message("assistant"):
        try:
            with st.spinner("正在处理…"):
                with urllib.request.urlopen(request, timeout=300) as response:
                    result = json.load(response)
            if "output" not in result:
                raise ValueError("Missing output")
            answer = result["output"].get("text", "服务返回了空回复。")
            st.session_state.session_id = result["output"].get("session_id")
            st.session_state.file_sent = True
            st.markdown(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})
        except urllib.error.HTTPError as exc:
            st.error(f"智能体调用失败（HTTP {exc.code}），请联系管理员检查服务日志。")
        except Exception:
            st.error("请求未完成，请稍后重试。")
