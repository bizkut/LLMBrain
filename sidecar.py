import os, json, asyncio
from fastapi import FastAPI, Request
from openai import AsyncOpenAI
import uvicorn

app = FastAPI()

# Configuration: Initialize the Asynchronous LLM Client
client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY"), 
    base_url="http://127.0.0.1:1234/v1"
)

# Global history tracker to prevent repetitive decisions
sim_history = {}

def extract_json(text):
    """Robustly extracts a single JSON object from potentially messy LLM output."""
    try:
        # Remove Markdown code blocks if present
        if "```" in text:
            blocks = text.split("```")
            for block in blocks:
                if "{" in block and "}" in block:
                    text = block
                    if text.strip().startswith("json"):
                        text = text.strip()[4:]
                    break
        
        start = text.find('{')
        end = text.rfind('}')
        if start == -1 or end == -1:
            return None
            
        json_str = text[start:end+1].strip()
        return json.loads(json_str)
    except Exception:
        return None

async def process_sim_logic(sim):
    """Encapsulates the decision-making logic for a single Sim."""
    sim_id = sim["id"]
    sim_name = sim["name"]
    
    if sim.get("is_sleeping"):
        return None
        
    if not sim.get("wants") and not any(v < 70 for v in sim.get("motives", {}).values()):
        return None

    current_wants = sorted(sim["wants"])
    is_executing = sim.get("is_llm_action_executing", False)
    is_queued = sim.get("is_llm_action_queued", False)
    
    if sim_id not in sim_history:
        sim_history[sim_id] = {'wants': [], 'cooldown': 0}
    history = sim_history[sim_id]

    if is_queued:
        return None

    if is_executing:
        if history['wants'] and history['wants'] != current_wants:
            return {"sim_id": sim_id, "action": "cancel"}
        print(f"🔄 {sim_name} is busy. Planning next step...")

    current_actions = sim.get("current_actions", [])
    for want in sim.get("wants", []):
        want_slug = want.lower().replace(" ", "")
        if any(want_slug in a.lower().replace("_", "") for a in current_actions):
            print(f"✅ {sim_name} is already fulfilling '{want}'.")
            return None

    if history['wants'] == current_wants and not any(v < 25 for v in sim.get("motives", {}).values()):
        history['cooldown'] += 1
        if history['cooldown'] < 3: 
            return None
    history['wants'] = current_wants
    history['cooldown'] = 0

    print(f"🧠 Thinking for {sim_name}...")
    motives_str = ", ".join([f"{k}: {v}%" for k, v in sim.get('motives', {}).items()])
    nearby_list = []
    for obj in sim.get('nearby_objects', [])[:12]:
        choices = ", ".join(list(obj.get('interactions', {}).keys()))
        nearby_list.append(f"- {obj['name']} (ID: {obj['id']}, Dist: {obj['dist']}m): [{choices}]")
    nearby_str = "\n".join(nearby_list)

    prompt = f"""
    Role: AI controlling Sim '{sim_name}' in The Sims 4.
    Mood: {sim['mood']}
    Moodlets (Context): {', '.join(sim.get('moodlets', []))}
    Points: {sim.get('satisfaction_points', 0)}
    Needs: {motives_str}
    Active Wants: {', '.join(sim['wants'])}
    
    Available Objects/Actions:
    {nearby_str}
    
    PRIORITY HIERARCHY (The Balanced Storyteller):
    1. SURVIVAL: If any Need is < 25%, fulfill immediately using objects with '[Satisfies: NEED]'.
    2. WHIMS/WANTS: If Needs > 25%, fulfill an 'Active Want'. You may purchase a Reward IF it helps a Want.
    3. EMOTION: If Mood is negative, pick an action to fix the feeling based on Moodlets.
    4. SELF-CARE: Address Needs < 70%.
    5. MAINTENANCE: Clean/Repair nearby dirty or broken objects.
    6. AUTONOMY: Pick a fun interaction fitting personality/Mood.

    Return ONLY JSON: {{"target_object_id": ID, "interaction_name": "EXACT_NAME", "reason": "Tier X - Why?", "priority": "high" or "low"}}
    """

    try:
        response = await client.chat.completions.create(
            model="Meta-Llama-3.1-8B-Instruct-abliterated-4bit",
            messages=[{"role": "user", "content": prompt}],
            timeout=45
        )
        content = response.choices[0].message.content
        decision = extract_json(content)
        
        if not decision:
            print(f"⚠️ Failed to parse JSON for {sim_name}. Output: {content[:100]}...")
            return None
            
        decision["sim_id"] = sim_id
        if "tier 1" in decision.get("reason", "").lower() or "survival" in decision.get("reason", "").lower():
            decision["priority"] = "high"
            
        readable = decision.get("interaction_name")
        target_id = decision.get("target_object_id")
        for obj in sim.get('nearby_objects', []):
            if obj.get('id') == target_id:
                mapping = obj.get('interactions', {})
                if readable in mapping:
                    decision["interaction_name"] = mapping[readable]
                    break
                    
        print(f"🎯 Decision for {sim_name}: {readable} ({decision.get('reason')})")
        return decision
    except Exception as e:
        print(f"❌ LLM Error for {sim_name}: {e}")
        return None

@app.post("/api/state")
async def receive_state(request: Request):
    state = await request.json()
    print("\n--- GAME TICK ---")
    commands = []

    for d in state.get("active_dialogs", []):
        choices = [f"Btn {r['id']}" for r in d.get("responses", [])]
        print(f"📞 Intercepted {d['tuning']} for {d['owner']}")
        prompt = f"Role: Sims 4 Controller. Event: Popup {d['tuning']} for {d['owner']}. Choices: {choices}. Goal: Pick best path. Return ONLY JSON: {{\"dialog_id\": {d['id']}, \"response_id\": ID, \"reason\": \"...\"}}"
        try:
            response = await client.chat.completions.create(
                model="Meta-Llama-3.1-8B-Instruct-abliterated-4bit",
                messages=[{"role": "user", "content": prompt}]
            )
            content = response.choices[0].message.content
            decision = extract_json(content)
            if decision:
                commands.append(decision)
                break
        except Exception as e:
            print(f"❌ Dialog Error: {e}")

    sim_tasks = [process_sim_logic(sim) for sim in state.get("sims", [])]
    results = await asyncio.gather(*sim_tasks)
    commands.extend([r for r in results if r])

    return {"status": "received", "commands": commands}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
