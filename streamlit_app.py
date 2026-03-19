"""
Streamlit UI for Medical Appointment Booking Agent
Frontend interface to interact with the FastAPI backend
"""
import streamlit as st
import requests
import json
from typing import List, Dict, Any

# Backend API URL
API_BASE_URL = "http://127.0.0.1:8000"

st.set_page_config(
    page_title="Medical Appointment Booking",
    page_icon="🏥",
    layout="wide"
)

st.title("🏥 Medical Appointment Booking Agent")
st.markdown("Chat with our AI assistant to book medical appointments")

# Initialize session state
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = None

# Sidebar for session info
with st.sidebar:
    st.header("Session Info")
    if st.session_state.session_id:
        st.write(f"Session ID: {st.session_state.session_id}")
    else:
        st.write("No active session")

    if st.button("New Chat"):
        st.session_state.messages = []
        st.session_state.session_id = None
        st.rerun()

    st.header("System Status")
    try:
        health_response = requests.get(f"{API_BASE_URL}/health", timeout=10)
        if health_response.status_code == 200:
            health_data = health_response.json()
            st.success("✅ Backend Connected")
            st.write(f"Ollama: {'✅ Running' if health_data.get('ollama') else '❌ Offline'}")
            st.write(f"MCP Tools: {health_data.get('mcp_tools', 0)}")
        else:
            st.error("❌ Backend Health Check Failed")
    except Exception as e:
        st.error(f"❌ Cannot connect to backend: {e}")
        st.info("💡 Make sure FastAPI server is running: `python run.py`")

# Chat interface
st.subheader("Chat")

# Display chat messages
chat_container = st.container()
with chat_container:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

# Chat input
if prompt := st.chat_input("Type your message here..."):
    # Add user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Send to backend with loading indicator
    with st.spinner("🤖 Thinking... "):
        try:
            payload = {
                "message": prompt,
                "session_id": st.session_state.session_id
            }
            # Request to /chat (non-streaming)
            response = requests.post(f"{API_BASE_URL}/chat", json=payload, timeout=120)
            response.raise_for_status()
            
            data = response.json()
            full_response = data.get("response", "")
            
            # Display assistant message
            with st.chat_message("assistant"):
                st.markdown(full_response)
            
            # Update session state and message history
            st.session_state.session_id = data.get("session_id")
            st.session_state.messages.append({"role": "assistant", "content": full_response})
            
            if data.get("appointment_booked"):
                st.success("✅ Appointment booked successfully!")
                booking_details = data.get("booking_details", {})
                if booking_details:
                    st.json(booking_details)

        except requests.exceptions.Timeout:
            error_msg = "⏰ Response timed out. The AI might be processing slowly. Please try again."
            st.session_state.messages.append({"role": "assistant", "content": error_msg})
            with st.chat_message("assistant"):
                st.error(error_msg)
        except requests.exceptions.ConnectionError:
            error_msg = "❌ Cannot connect to backend. Please ensure the FastAPI server is running on port 8000."
            st.session_state.messages.append({"role": "assistant", "content": error_msg})
            with st.chat_message("assistant"):
                st.error(error_msg)
        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            st.session_state.messages.append({"role": "assistant", "content": error_msg})
            with st.chat_message("assistant"):
                st.error(error_msg)

# Footer
st.markdown("---")
st.markdown("Built with Streamlit + FastAPI + Ollama")