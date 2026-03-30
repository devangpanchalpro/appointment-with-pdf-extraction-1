"""
Streamlit UI for Medical QA and Appointment Booking
Frontend interface to interact with medical chatbot and appointment booking
"""
import streamlit as st
import requests
import json
from typing import List, Dict, Any
from medical_qa.chat_engine import build_chat_answer

# Backend API URL
API_BASE_URL = "http://localhost:8001"

st.set_page_config(
    page_title="AarogyaOne - Medical Assistant",
    page_icon="🏥",
    layout="wide"
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
    .home-button {
        display: flex;
        gap: 20px;
        justify-content: center;
        margin-top: 40px;
    }
    .button-container {
        display: flex;
        gap: 20px;
        justify-content: center;
        margin-bottom: 20px;
    }
</style>
""", unsafe_allow_html=True)

# Initialize session state
if "current_page" not in st.session_state:
    st.session_state.current_page = "home"
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "abha" not in st.session_state:
    st.session_state.abha = ""
if "qa_history" not in st.session_state:
    st.session_state.qa_history = []

# Sidebar
with st.sidebar:
    st.header("🏥 AarogyaOne")
    
    if st.button("🏠 Home", use_container_width=True):
        st.session_state.current_page = "home"
        st.rerun()
    
    st.divider()
    st.write("### System Status")
    try:
        health_response = requests.get(f"{API_BASE_URL}/health", timeout=5)
        if health_response.status_code == 200:
            st.success("✅ Backend Connected")
        else:
            st.error("❌ Backend Down")
    except:
        st.warning("⚠️ Backend Not Available")


# ===== HOME PAGE =====
if st.session_state.current_page == "home":
    col1, col2 = st.columns([1, 8])
    with col1:
        st.markdown('<div style="font-size: 50px; line-height: 1; margin-left: 20px;">🏥</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<h2 class="main-title">AarogyaOne Medical Assistant</h2>', unsafe_allow_html=True)
    
    st.markdown("---")
    st.markdown("<h3 style='text-align: center;'>Choose a Service</h3>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        col_a, col_b = st.columns(2)
        
        with col_a:
            if st.button("📅 Appointment Booking", use_container_width=True, key="btn_appointment"):
                st.session_state.current_page = "appointment"
                st.rerun()
        
        with col_b:
            if st.button("💬 Medical Chatbot", use_container_width=True, key="btn_chatbot"):
                st.session_state.current_page = "chatbot"
                st.rerun()

    st.markdown("---")
    st.info("👈 Click on one of the buttons above to get started!")


# ===== APPOINTMENT BOOKING PAGE =====
elif st.session_state.current_page == "appointment":
    col1, col2 = st.columns([1, 8])
    with col1:
        st.markdown('<div style="font-size: 50px; line-height: 1; margin-left: 20px;">📅</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<h2 class="main-title">Appointment Booking</h2>', unsafe_allow_html=True)

    if st.session_state.session_id:
        st.info(f"✅ Session Active: {st.session_state.session_id[:8]}...")
    
    if st.button("🔄 New Chat", use_container_width=False):
        st.session_state.messages = []
        st.session_state.session_id = None
        st.rerun()

    st.markdown("---")

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


# ===== MEDICAL CHATBOT PAGE =====
elif st.session_state.current_page == "chatbot":
    col1, col2 = st.columns([1, 8])
    with col1:
        st.markdown('<div style="font-size: 50px; line-height: 1; margin-left: 20px;">💬</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<h2 class="main-title">Medical Chatbot</h2>', unsafe_allow_html=True)

    # Sidebar for Medical QA
    with st.sidebar:
        st.divider()
        st.subheader("👤 Patient Info")
        st.session_state.abha = st.text_input(
            "ABHA ID (Optional)", 
            value=st.session_state.abha,
            placeholder="Enter your ABHA ID to access documents"
        )
        
        if st.button("🔄 Clear Chat", use_container_width=True):
            st.session_state.qa_history = []
            st.rerun()

    st.info("💡 Ask me anything about medical topics or your uploaded documents!")
    st.markdown("---")

    # Chat interface
    chat_container = st.container()
    with chat_container:
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    # Chat input
    if prompt := st.chat_input("Ask a medical question..."):
        # Add user message
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Process with Medical QA engine
        with st.spinner("Analyzing your question..."):
            try:
                # Convert messages to history format for build_chat_answer
                history = []
                for i in range(0, len(st.session_state.messages) - 1, 2):
                    if i + 1 < len(st.session_state.messages):
                        user_msg = st.session_state.messages[i].get("content", "")
                        assist_msg = st.session_state.messages[i + 1].get("content", "")
                        history.append((user_msg, assist_msg))
                
                # Get answer from medical QA engine
                answer = build_chat_answer(
                    query=prompt,
                    abha=st.session_state.abha,
                    history=history
                )
                
                # Display assistant message
                with st.chat_message("assistant"):
                    st.markdown(answer)
                
                # Update session state
                st.session_state.messages.append({"role": "assistant", "content": answer})
                st.session_state.qa_history.append((prompt, answer))

            except Exception as e:
                error_msg = f"❌ Error: {str(e)}"
                st.session_state.messages.append({"role": "assistant", "content": error_msg})
                with st.chat_message("assistant"):
                    st.error(error_msg)