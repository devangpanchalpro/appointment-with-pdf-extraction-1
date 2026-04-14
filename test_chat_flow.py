"""
End-to-end test of the /chat agent using the 4-step MCP flow.
Simulates a full booking conversation without HTTP/JWT.
"""
import asyncio
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def test_chat_flow():
    from app.agent.agent import appointment_agent

    session_id = "test-session-001"

    async def chat(msg):
        print(f"\n{'='*60}")
        print(f"USER: {msg}")
        print(f"{'='*60}")
        result = await appointment_agent.chat(session_id, msg)
        print(f"\nBOT:\n{result['response']}")
        print(f"\n[booked={result['appointment_booked']}]")
        return result

    # Step 1: Start booking → should fetch doctors via MCP Tool 1
    await chat("I want to book an appointment")

    # Step 2: Select doctor #1 → should fetch facilities via MCP Tool 2
    await chat("1")

    # Step 3: If single facility auto-selected → slots already shown via MCP Tool 3
    #          If multiple → select facility, then slots shown

    # Step 4: Select slot #1
    await chat("1")

    # Step 5: Provide patient details
    await chat("Name: Test Patient, DOB: 15-05-1995, Gender: Male, Mobile: 9876543210, Address: Ahmedabad Gujarat")

    # Step 6: Confirm booking
    result = await chat("Yes")

    print(f"\n{'='*60}")
    print("TEST COMPLETE")
    print(f"Appointment booked: {result['appointment_booked']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(test_chat_flow())
