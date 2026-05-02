import services
import sims4.commands
import alarms
import threading
import queue
import urllib.request
import json
import interactions.context
import interactions.priority
import ui.ui_dialog_service
import zone
from functools import wraps
from date_and_time import create_time_span

# --- Configuration ---
POLLING_INTERVAL_MINUTES = 10
SIDECAR_URL = "http://127.0.0.1:8000/api/state"

# --- Thread-Safe State & Queues ---
state_lock = threading.Lock()
outgoing_queue = queue.Queue()
incoming_queue = queue.Queue()

ACTIVE_LLM_ACTIONS = {}
ACTIVE_DIALOGS = {}
STATUS = {
    "brain": "No runs yet",
    "net": "No requests yet",
    "action": "Idle"
}

bg_thread = None
brain_alarm = None

class _AlarmOwner:
    pass
alarm_owner = _AlarmOwner()

# --- Helper Functions ---

def get_localized_string_context(ls):
    """Safely extracts data from localized string protocols."""
    if ls is None:
        return {"hash": 0, "tokens": []}
    tokens = []
    try:
        raw_tokens = getattr(ls, 'tokens', [])
        for t in raw_tokens:
            if hasattr(t, 'raw_text'):
                tokens.append(str(t.raw_text))
            elif hasattr(t, 'number'):
                tokens.append(str(t.number))
    except:
        pass
    return {"hash": getattr(ls, 'hash', 0), "tokens": tokens}

def extract_game_state():
    """Compiles the full game state for the LLM."""
    client = services.client_manager().get_first_client()
    if not client:
        return None
        
    state = {"sims": [], "active_dialogs": []}
    dialog_service = services.ui_dialog_service()
    
    with state_lock:
        # 1. Dialog Interception (Phone calls, popups)
        if dialog_service is not None:
            for d_id, dialog in list(ACTIVE_DIALOGS.items()):
                try:
                    if d_id not in dialog_service._active_dialogs:
                        ACTIVE_DIALOGS.pop(d_id, None)
                        continue
                    
                    # Optimization: Auto-respond to single-button popups locally
                    responses = list(dialog._get_responses_gen()) if hasattr(dialog, '_get_responses_gen') else getattr(dialog, 'responses', [])
                    if len(responses) == 1:
                        dialog_service.dialog_respond(d_id, responses[0].dialog_response_id)
                        ACTIVE_DIALOGS.pop(d_id, None)
                        continue

                    # Determine Owner Name safely
                    owner_name = "System"
                    if dialog.owner is not None:
                        try:
                            owner_name = (f"{dialog.owner.sim_info.first_name} {dialog.owner.sim_info.last_name}").strip()
                        except:
                            owner_name = "Unknown Sim"

                    state["active_dialogs"].append({
                        "id": d_id,
                        "owner": owner_name,
                        "tuning": dialog.__class__.__name__,
                        "is_urgent": dialog.get_phone_ring_type() == 0,
                        "responses": [{"id": r.dialog_response_id, "text": "Choice"} for r in responses]
                    })
                except:
                    continue

        # 2. Sim State Extraction
        for sim_info in client.selectable_sims:
            sim = sim_info.get_sim_instance()
            # Skip if not instanced or not on the current lot
            if not sim or not sim.is_on_active_lot():
                continue
                
            # HARDENING: More robust name extraction
            try:
                sim_name = (f"{sim_info.first_name} {sim_info.last_name}").strip()
            except:
                sim_name = f"Sim_{sim.id}"
                
            if not sim_name or sim_name.lower() == "none":
                sim_name = f"Sim_{sim.id}"

            sim_data = {
                "id": sim.id,
                "name": sim_name,
                "mood": sim.get_mood().__name__ if sim.get_mood() else "Fine",
                "is_sleeping": getattr(sim, 'sleeping', False) or any('sleep' in si.__class__.__name__.lower() for si in (sim.si_state or [])),
                "motives": {},
                "wants": [],
                "moodlets": [],
                "current_actions": [],
                "satisfaction_points": sim_info.get_satisfaction_points(),
                "is_llm_action_executing": False,
                "is_llm_action_queued": False,
                "nearby_objects": []
            }
            
            # Moodlets & Reasons
            buff_component = getattr(sim_info, 'Buffs', None)
            if buff_component:
                for buff in buff_component:
                    if getattr(buff, 'visible', False):
                        b_name = getattr(buff, 'buff_type', buff.__class__).__name__.replace('Buff_', '').replace('buff_', '')
                        import re
                        clean_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', b_name).strip().title()
                        
                        # Add Reason Context
                        reason = ""
                        b_reason = getattr(buff, '_buff_reason', None)
                        if b_reason:
                            ctx = get_localized_string_context(b_reason)
                            if ctx["tokens"]: reason = f" (Source: {', '.join(ctx['tokens'])})"
                        
                        sim_data["moodlets"].append(f"{clean_name}{reason}")

            # Motives (Needs)
            tracker = getattr(sim, 'commodity_tracker', None)
            if tracker:
                for stat in tracker:
                    if getattr(stat, 'visible', False) and getattr(stat, 'ui_sort_order', 0) > 0:
                        m_name = stat.__class__.__name__.replace('motive_', '').replace('Motive_', '')
                        val, min_v, max_v = stat.get_value(), getattr(stat, 'min_value', -100), getattr(stat, 'max_value', 100)
                        sim_data["motives"][m_name] = round(((val - min_v) / (max_v - min_v) * 100), 1) if max_v > min_v else 50

            # Active Wants (Filtered)
            if sim_info.whim_tracker is not None:
                for slot in getattr(sim_info.whim_tracker, '_whim_slots', []):
                    whim = getattr(slot, 'whim', None)
                    if whim:
                        w_name = whim.__name__
                        if not any(x in w_name.lower() for x in ['_buy', '_purchase', '_build', '_addroom']):
                            clean_w = re.sub(r'(?<!^)(?=[A-Z])', ' ', w_name.replace('whim_', '').replace('Whim_', '')).strip().title()
                            sim_data["wants"].append(clean_w)

            # LLM Status tracking
            if sim.si_state:
                sim_data["current_actions"] = [si.__class__.__name__ for si in sim.si_state if not any(x in si.__class__.__name__.lower() for x in ['idle', 'posture', 'stand'])]
                
            llm_action = ACTIVE_LLM_ACTIONS.get(sim.id)
            if llm_action:
                sim_data["is_llm_action_executing"] = sim.si_state is not None and llm_action in sim.si_state
                sim_data["is_llm_action_queued"] = getattr(sim, 'queue', None) is not None and llm_action in sim.queue
                if not sim_data["is_llm_action_executing"] and not sim_data["is_llm_action_queued"]:
                    ACTIVE_LLM_ACTIONS.pop(sim.id, None)

            # Object & Interaction Scan
            scan_ctx = interactions.context.InteractionContext(sim, interactions.context.InteractionContext.SOURCE_AUTONOMY, interactions.priority.Priority.High)
            objs = []
            for obj in services.object_manager().get_all():
                if obj.id == sim.id or not getattr(obj, 'visible_to_client', True) or getattr(obj, 'parent', None): continue
                
                affs = {}
                try:
                    raw_affs = list(obj.super_affordances(context=scan_ctx)) if callable(getattr(obj, 'super_affordances', None)) else getattr(obj, '_super_affordances', [])
                    for a in raw_affs:
                        if len(affs) >= 8: break
                        if not hasattr(a, 'display_name') or 'debug' in a.__name__.lower() or len(a.__name__) < 5: continue
                        
                        # Need Tagging
                        tags = [n for n, kw in [("Hunger", ["hunger", "eat", "fridge", "cook"]), ("Energy", ["energy", "sleep", "nap", "bed"]), ("Bladder", ["bladder", "toilet", "pee"]), ("Hygiene", ["hygiene", "shower", "bath"]), ("Social", ["social", "chat", "talk"]), ("Fun", ["fun", "play", "game"])] if any(k in a.__name__.lower() for k in kw)]
                        name = re.sub(r'(?<!^)(?=[A-Z])', ' ', a.__name__.replace('_', ' ')).strip().title()
                        if tags: name += f" [Satisfies: {', '.join(tags)}]"
                        affs[name] = a.__name__
                except: continue
                
                if affs:
                    dist = ((sim.position.x - obj.position.x)**2 + (sim.position.z - obj.position.z)**2)**0.5
                    objs.append({"id": obj.id, "name": getattr(obj.definition, 'name', obj.__class__.__name__), "dist": round(dist, 1), "interactions": affs})
            
            # Add Rewards Store if points available
            if sim_data["satisfaction_points"] > 0 and getattr(sim_info, '_satisfaction_tracker', None):
                rewards = {}
                for reward, data in list(sim_info._satisfaction_tracker.SATISFACTION_STORE_ITEMS.items())[:15]:
                    if data.cost <= sim_data["satisfaction_points"] and reward.is_valid(sim_info):
                        r_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', reward.__name__.replace('reward_', '')).strip().title()
                        rewards[f"Buy {r_name} ({data.cost}pts)"] = f"PURCHASE_{reward.guid64}"
                if rewards:
                    objs.append({"id": 999, "name": "Rewards Store", "dist": 0.0, "interactions": rewards})

            sim_data["nearby_objects"] = sorted(objs, key=lambda x: x['dist'])[:20]
            state["sims"].append(sim_data)
            
    return state

# --- Background Networking ---

def network_worker():
    global STATUS
    while True:
        state = outgoing_queue.get()
        if state is None: break
        try:
            req = urllib.request.Request(SIDECAR_URL, method="POST")
            req.add_header('Content-Type', 'application/json; charset=utf-8')
            jsondata = json.dumps(state).encode('utf-8')
            response = urllib.request.urlopen(req, jsondata, timeout=60)
            resp_data = json.loads(response.read().decode('utf-8'))
            incoming_queue.put(resp_data)
            STATUS["net"] = "OK"
        except Exception as e:
            STATUS["net"] = f"Error: {e}"

# --- Command Execution ---

def execute_command(cmd):
    global STATUS
    try:
        with state_lock:
            # 1. Dialog Response
            if "dialog_id" in cmd:
                d_id, r_id = int(cmd["dialog_id"]), int(cmd["response_id"])
                if d_id in ACTIVE_DIALOGS:
                    services.ui_dialog_service().dialog_respond(d_id, r_id)
                    ACTIVE_DIALOGS.pop(d_id, None)
                    STATUS["action"] = f"Responded to Dialog {d_id}"
                return

            sim_id = int(cmd.get("sim_id", 0))
            sim_info = services.sim_info_manager().get(sim_id)
            sim = sim_info.get_sim_instance() if sim_info else None
            if not sim: return

            # 2. Cancellation
            if cmd.get("action") == "cancel":
                existing = ACTIVE_LLM_ACTIONS.pop(sim_id, None)
                if existing: existing.cancel(1, "LLM Cancel")
                return

            # 3. Busy & Priority Logic
            is_high = str(cmd.get("priority", "")).lower() == "high"
            existing = ACTIVE_LLM_ACTIONS.get(sim_id)
            if existing:
                if (sim.si_state and existing in sim.si_state) or (getattr(sim, 'queue', None) and existing in sim.queue):
                    if not is_high: return # Busy
                    try: existing.cancel(1, "LLM Override")
                    except: pass
                ACTIVE_LLM_ACTIONS.pop(sim_id, None)

            # 4. Rewards Purchase
            interaction_name = str(cmd.get("interaction_name", ""))
            if interaction_name.startswith("PURCHASE_"):
                tracker = getattr(sim_info, '_satisfaction_tracker', None)
                if tracker:
                    tracker.purchase_satisfaction_reward(int(interaction_name.split("_")[1]))
                    STATUS["action"] = f"Purchased Reward for {sim_id}"
                return

            # 5. Push Interaction
            target_id = int(cmd.get("target_object_id", 0))
            target = services.object_manager().get(target_id)
            if not target:
                t_info = services.sim_info_manager().get(target_id)
                target = t_info.get_sim_instance() if t_info else None
            if not target: return

            # Find Affordance
            scan_ctx = interactions.context.InteractionContext(sim, interactions.context.InteractionContext.SOURCE_AUTONOMY, interactions.priority.Priority.High)
            affs = list(target.super_affordances(context=scan_ctx)) if callable(getattr(target, 'super_affordances', None)) else getattr(target, '_super_affordances', [])
            
            # Score matching
            scored = []
            search = interaction_name.lower().replace(" ", "_")
            for a in affs:
                a_name = a.__name__.lower()
                score = 10 if search in a_name else 0
                score += sum(2 for w in search.split("_") if len(w) > 3 and w in a_name)
                if score > 0: scored.append((score, a))
            
            if not scored: return
            scored.sort(key=lambda x: x[0], reverse=True)
            affordance = scored[0][1]

            # Execution
            insert = interactions.context.QueueInsertStrategy.FIRST if is_high else interactions.context.QueueInsertStrategy.LAST
            priority = interactions.priority.Priority.Critical if is_high else interactions.priority.Priority.High
            
            if is_high and sim.si_state:
                for si in list(sim.si_state):
                    try: si.cancel(1, "Survival Override")
                    except: pass

            ctx = interactions.context.InteractionContext(sim, interactions.context.InteractionContext.SOURCE_AUTONOMY, priority, insert_strategy=insert)
            result = sim.push_super_affordance(affordance, target, ctx)
            if result:
                ACTIVE_LLM_ACTIONS[sim_id] = result.interaction
                STATUS["action"] = f"Queued {affordance.__name__}"
    except Exception as e:
        STATUS["brain"] = f"Exec Error: {e}"

# --- Game Loops ---

def brain_tick(_):
    global STATUS
    try:
        while not incoming_queue.empty():
            try: execute_command(incoming_queue.get_nowait())
            except: break
        
        state = extract_game_state()
        if state:
            with outgoing_queue.mutex: outgoing_queue.queue.clear()
            outgoing_queue.put(state)
        STATUS["brain"] = "OK"
    except Exception as e:
        import traceback
        STATUS["brain"] = traceback.format_exc()

@sims4.commands.Command('llm.start', command_type=sims4.commands.CommandType.Live)
def start_llm_mod(_connection=None):
    output = sims4.commands.CheatOutput(_connection)
    global bg_thread, brain_alarm
    if bg_thread is None or not bg_thread.is_alive():
        bg_thread = threading.Thread(target=network_worker, daemon=True)
        bg_thread.start()
    if brain_alarm is None:
        brain_alarm = alarms.add_alarm(alarm_owner, create_time_span(minutes=POLLING_INTERVAL_MINUTES), brain_tick, repeating=True)
        brain_tick(None)
    output("LLM Brain Started.")

@sims4.commands.Command('llm.status', command_type=sims4.commands.CommandType.Live)
def status_llm_mod(_connection=None):
    output = sims4.commands.CheatOutput(_connection)
    output(f"--- LLM Mod Status ---\nBrain: {STATUS['brain']}\nNet: {STATUS['net']}\nAction: {STATUS['action']}")

# --- Hooks & Injection ---

def inject_to(target_object, target_function_name):
    def _inject_to(new_function):
        target_function = getattr(target_object, target_function_name)
        @wraps(target_function)
        def _inject(*args, **kwargs): return new_function(target_function, *args, **kwargs)
        setattr(target_object, target_function_name, _inject)
        return new_function
    return _inject_to

@inject_to(ui.ui_dialog_service.UiDialogService, 'dialog_show')
def llm_on_dialog_show(original, self, dialog, phone_ring_type, *args, **kwargs):
    # HARDENING: Skip passive notifications and empty dialogs
    d_name = dialog.__class__.__name__
    if d_name == "UiDialogNotification":
        return original(self, dialog, phone_ring_type, *args, **kwargs)
        
    responses = list(dialog._get_responses_gen()) if hasattr(dialog, '_get_responses_gen') else getattr(dialog, 'responses', [])
    if not responses:
        return original(self, dialog, phone_ring_type, *args, **kwargs)

    with state_lock: 
        ACTIVE_DIALOGS[dialog.dialog_id] = dialog
    
    # URGENT: Don't wait for the next tick! Trigger an immediate state send
    state = extract_game_state()
    if state:
        with outgoing_queue.mutex: outgoing_queue.queue.clear()
        outgoing_queue.put(state)

    return original(self, dialog, phone_ring_type, *args, **kwargs)

@inject_to(zone.Zone, 'on_loading_screen_animation_finished')
def llm_on_zone_load(original, self, *args, **kwargs):
    res = original(self, *args, **kwargs)
    global brain_alarm
    if brain_alarm:
        brain_alarm = alarms.add_alarm(alarm_owner, create_time_span(minutes=POLLING_INTERVAL_MINUTES), brain_tick, repeating=True)
        brain_tick(None)
    return res
