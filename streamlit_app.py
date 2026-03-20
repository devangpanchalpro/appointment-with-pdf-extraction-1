"""
Streamlit UI for Medical Appointment Booking Agent
Frontend interface to interact with the FastAPI backend
"""
import streamlit as st
import requests
import json
from typing import List, Dict, Any

# Backend API URL
API_BASE_URL = "http://localhost:8001"

st.set_page_config(
    page_title="Medical Appointment Booking",
    page_icon="🏥",
    layout="centered"
)
                                        
# Custom CSS for Premium Look
st.markdown("""
<style>
    .main {
        background-color: #f8f9fa;
    }
   
    .stChatMessage {
        border-radius: 15px;
        padding: 1rem;
        margin-bottom: 1rem;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    .stChatMessage.user {
        background-color: #e3f2fd;
        border: 1px solid #bbdefb;
    }
    .stChatMessage.assistant {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
    }
    .main-title{
        color:white;
        font-family: 'Inter', sans-serif;
        font-weight: bold;      
        margin:0;                     
        font-size: 3rem;
    }
    .booking-result {
        background-color: #e8f5e9;
        border: 1px solid #c8e6c9;
        border-radius: 10px;
        padding: 1.5rem;
        margin-top: 1rem;
    }
    .booking-field {
        font-weight: bold;
        color: #2e7d32;
    }
</style>
""", unsafe_allow_html=True)

# Header with Logo and Title
col1, col2 = st.columns([1, 8])
with col1:
    st.markdown('<div style="font-size: 50px; line-height: 1; margin-left: 20px;">🏥</div>', unsafe_allow_html=True)
with col2:
    st.markdown('<h2 class="main-title">AarogyaOne Appointment Booking Agent</h2>', unsafe_allow_html=True)

# Initialize session state
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = None

# Sidebar restoration
with st.sidebar:
    st.header("🏥 AarogyaOne")
    if st.session_state.session_id:
        st.info(f"Session Active: {st.session_state.session_id[:8]}...")
    else:
        st.write("No active session")
        
    if st.button("🔄 New Chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.session_id = None
        st.rerun()
    
    st.divider()
    st.write("### System Status")
    try:
        health_response = requests.get(f"{API_BASE_URL}/health", timeout=5)
        if health_response.status_code == 200:
            st.success("✅ Connected")
        else:
            st.error("❌ Link Down")
    except:
        st.error("❌ Gateway Timeout")

# Chat interface
chat_container = st.container()
with chat_container:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

# Chat input
if prompt := st.chat_input("How can I help you book an appointment today?"):
    # Add user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Send to backend with loading indicator
    with st.spinner("Processing..."):
        try:
            payload = {
                "message": prompt,
                "session_id": st.session_state.session_id
            }
            response = requests.post(f"{API_BASE_URL}/chat", json=payload, timeout=120)
            response.raise_for_status()
            
            data = response.json()
            full_response = data.get("response", "")
            
            # Display assistant message
            with st.chat_message("assistant"):
                st.markdown(full_response)
            
            # Update session state
            st.session_state.session_id = data.get("session_id")
            st.session_state.messages.append({"role": "assistant", "content": full_response})
            
            if data.get("appointment_booked"):
                details = data.get("booking_details", {})
                res = details.get("result", {}) if "result" in details else details
                
                st.markdown(f"""
                <div class="booking-result">
                    <h4>✅ Appointment Confirmed</h4>
                    <p><span class="booking-field">token</span> - {res.get('token', 'N/A')}</p>
                    <p><span class="booking-field">patientId</span> - {res.get('patientId', 'N/A')}</p>
                    <p><span class="booking-field">facilityId</span> - {res.get('facilityId', 'N/A')}</p>
                    <p><span class="booking-field">appointmentId</span> - {res.get('appointmentId', 'N/A')}</p>
                </div>
                """, unsafe_allow_html=True)

        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            st.session_state.messages.append({"role": "assistant", "content": error_msg})
            with st.chat_message("assistant"):
                st.error(error_msg)

# Footer removed per user request