import streamlit as st
import requests
import json

st.set_page_config(page_title="GoodBooks AI Assistant", page_icon="💬", layout="centered")

# ============================================================
# SIDEBAR — Configuration
# ============================================================
with st.sidebar:
    st.title("Chat interface")

    api_url = st.text_input(
        "Backend API URL",
        value="http://localhost:8000/gbaiapi/chat_Interface",
        help="URL of the FastAPI backend"
    )

    login_header = st.text_area(
        "Login Header (JSON)",
        height=180,
        placeholder='{"UserId": 123, "UserName": "John", "BaseUri": "server:81", "FEUri": "http://server:92/", ...}',
        help="Paste the full Login JSON from your GoodBooks session"
    )

    if st.button("Clear Chat"):
        st.session_state.messages = []
        st.rerun()

    st.markdown("---")
    st.caption("GoodBooks AI Assistant")
    st.caption("Supports: Leave | Time Slip | Pack")

# ============================================================
# CHAT STATE
# ============================================================
if "messages" not in st.session_state:
    st.session_state.messages = []

# ============================================================
# HEADER
# ============================================================
st.markdown(
    "<h2 style='text-align:center; color:#1a73e8;'>GoodBooks AI Assistant</h2>",
    unsafe_allow_html=True
)
st.markdown(
    "<p style='text-align:center; color:#666; margin-top:-10px;'>Apply Leave &nbsp;|&nbsp; Submit Time Slip &nbsp;|&nbsp; Create Pack</p>",
    unsafe_allow_html=True
)
st.divider()

# ============================================================
# CHAT HISTORY
# ============================================================
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ============================================================
# CHAT INPUT
# ============================================================
user_input = st.chat_input("Type your message... (e.g. Apply leave, Submit time slip)")

if user_input:
    # Show user message
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Build headers
    headers = {"Content-Type": "application/json"}
    if login_header.strip():
        try:
            json.loads(login_header.strip())  # validate JSON
            headers["Login"] = login_header.strip()
        except json.JSONDecodeError:
            st.warning("Login header is not valid JSON — sending without it.")

    # Call backend
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                response = requests.post(
                    api_url,
                    json={"message": user_input},
                    headers=headers,
                    timeout=60
                )
                response.raise_for_status()
                data = response.json()
                reply = data.get("message") or "No response from server."

            except requests.exceptions.ConnectionError:
                reply = "Cannot connect to the backend. Make sure the server is running."
            except requests.exceptions.Timeout:
                reply = "Server took too long to respond. Please try again."
            except requests.exceptions.HTTPError as e:
                reply = f"Server error: {e.response.status_code}"
            except Exception as e:
                reply = f"Unexpected error: {str(e)}"

        st.markdown(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
