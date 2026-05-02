import services
import sims4.commands
import alarms
from date_and_time import create_time_span
import threading
import queue
import urllib.request
import json
import interactions.context
import interactions.priority

POLLING_INTERVAL_MINUTES = 10
SIDECAR_URL = "http://127.0.0.1:8000/api/state"

# Thread-safe queues for communication between the game thread and network thread
outgoing_queue = queue.Queue()
incoming_queue = queue.Queue()

bg_thread = None
brain_alarm = None

ACTIVE_LLM_ACTIONS = {}

# The game's alarm system requires an object that supports weak references as the 'owner'.
class _AlarmOwner:
    pass
alarm_owner = _AlarmOwner()

LAST_BRAIN_ERROR = "No runs yet"
LAST_NETWORK_ERROR = "No requests yet"
LAST_ACTION_STATUS = "No actions pushed yet"

def extract_game_state():
    """Extracts basic information from controllable Sims."""
    client = services.client_manager().get_first_client()
    if not client:
        return None
        
    active_sims = client.selectable_sims
    state = {"sims": []}
    
    for sim_info in active_sims:
        sim = sim_info.get_sim_instance()
        if not sim:
            continue
            
        # Extract Wants (Whims) and Mood
        wants = []
        mood = "Unknown"
        
        try:
            # Grab the current mood
            current_mood = sim.get_mood()
            if current_mood is not None:
                mood = current_mood.__name__
                
            # Detect if Sim is sleeping
            is_sleeping = getattr(sim, 'sleeping', False)
            # Also check if any active interaction has "sleep" in the name
            if not is_sleeping and sim.si_state is not None:
                for si in sim.si_state:
                    if 'sleep' in si.__class__.__name__.lower() or 'nap' in si.__class__.__name__.lower():
                        is_sleeping = True
                        break
                
            if sim_info.whim_tracker is not None:
                # Extract Wants directly from the internal _whim_slots list
                whim_slots = getattr(sim_info.whim_tracker, '_whim_slots', [])
                for slot in whim_slots:
                    whim_class = getattr(slot, 'whim', None)
                    if whim_class is not None:
                        # Skip Build Mode / Purchase whims that Sims can't do themselves
                        goal_type = getattr(whim_class, 'goal', None)
                        goal_name = goal_type.__name__ if goal_type else ""
                        
                        is_build_whim = False
                        if "PurchasedObject" in goal_name or "LotTileCount" in goal_name:
                            is_build_whim = True
                        
                        # Also check the whim name for keywords just in case
                        whim_name = whim_class.__name__
                        if any(x in whim_name.lower() for x in ['_buy', '_purchase', '_addroom', '_build']):
                            is_build_whim = True
                            
                        if is_build_whim:
                            continue
                            
                        # Clean up class name: "whim_PlayPiano" -> "Play Piano"
                        name = whim_name
                        if name.lower().startswith('whim_'):
                            name = name[5:]
                        # Add spaces before capital letters
                        readable_name = "".join([" " + c if c.isupper() else c for c in name]).strip()
                        wants.append(readable_name)
                        
            # Check if the LLM action is currently running or in the queue
            has_llm_action = False
            current_actions = []
            
            if sim.si_state is not None:
                for si in sim.si_state:
                    si_name = si.__class__.__name__
                    # Skip common idle/posture interactions to focus on "real" actions
                    if any(x in si_name.lower() for x in ['idle', 'posture', 'stand', 'wait']):
                        continue
                    current_actions.append(si_name)
                    
            llm_action = ACTIVE_LLM_ACTIONS.get(sim.id)
            is_llm_action_executing = False
            is_llm_action_queued = False
            
            if llm_action is not None:
                is_llm_action_executing = sim.si_state is not None and llm_action in sim.si_state
                is_llm_action_queued = getattr(sim, 'queue', None) is not None and llm_action in sim.queue
                
                if not is_llm_action_executing and not is_llm_action_queued:
                    ACTIVE_LLM_ACTIONS.pop(sim.id, None)
        except Exception as e:
            wants.append(f"Error: {e}")
            
        # Extract Interactive Objects across the whole lot
        nearby_objects = []
        try:
            sim_pos = getattr(sim, 'position', None)
            if sim_pos is not None:
                for obj in services.object_manager().get_all():
                    if obj.id == sim.id:
                        continue
                        
                    try:
                        # Skip objects hidden in inventories
                        if getattr(obj, 'parent', None) is not None:
                            continue
                            
                        obj_pos = getattr(obj, 'position', None)
                        if obj_pos is None:
                            continue
                            
                        # Calculate distance squared
                        dist_sq = (sim_pos.x - obj_pos.x)**2 + (sim_pos.y - obj_pos.y)**2 + (sim_pos.z - obj_pos.z)**2
                        
                        # We no longer limit by distance (dist_sq < 100), 
                        # allowing Sims to see objects across the whole lot.
                        
                        # Retrieve Affordances
                        affordance_attr = getattr(obj, 'super_affordances', None)
                        if callable(affordance_attr):
                            try:
                                affordances = list(obj.super_affordances())
                            except Exception:
                                affordances = getattr(obj, '_super_affordances', [])
                        else:
                            affordances = getattr(obj, '_super_affordances', [])
                            
                        if not affordances:
                            continue
                            
                        if getattr(obj, 'is_sim', False):
                            obj_name = f"Sim: {getattr(obj.sim_info, 'first_name', '')}"
                        else:
                            obj_name = getattr(obj.definition, 'name', None) if hasattr(obj, 'definition') else None
                            if not obj_name:
                                obj_name = obj.__class__.__name__
                                
                        # Extract meaningful interactions
                        available_interactions = {}
                        for aff in affordances:
                            # Cap at 10 interactions per object
                            if len(available_interactions) >= 10:
                                break
                                
                            aff_name = getattr(aff, '__name__', '')
                            if not aff_name or aff_name.startswith('debug_') or 'cheat' in aff_name.lower():
                                continue
                            if len(aff_name) < 5:
                                continue
                                
                            import re
                            temp_name = aff_name.replace('_', ' ')
                            temp_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', temp_name)
                            readable_name = temp_name.strip().title()
                            available_interactions[readable_name] = aff_name
                                    
                        if available_interactions:
                            nearby_objects.append({
                                "id": obj.id,
                                "name": obj_name,
                                "dist": round(dist_sq**0.5, 1),
                                "interactions": available_interactions
                            })
                    except Exception:
                        continue
            
            # Sort by distance so the LLM sees the closest things first,
            # but we now cap at 25 objects to allow for lot-wide scanning.
            nearby_objects.sort(key=lambda x: x.get("dist", 999))
            nearby_objects = nearby_objects[:25]
        except Exception as e:
            nearby_objects.append({"error": str(e)})
            
        # Extract Motives (Needs)
        motives = {}
        try:
            # Commodities include Hunger, Energy, Bladder, etc.
            tracker = getattr(sim, 'commodity_tracker', None)
            if tracker is not None:
                for stat in tracker:
                    # Filter for visible motives that typically appear in the UI
                    if getattr(stat, 'visible', False) and getattr(stat, 'ui_sort_order', 0) > 0:
                        stat_name = stat.__class__.__name__
                        if stat_name.startswith('motive_'):
                            stat_name = stat_name[7:]
                        
                        val = stat.get_value()
                        min_val = getattr(stat, 'min_value', -100)
                        max_val = getattr(stat, 'max_value', 100)
                        
                        # Normalize to 0-100 scale for LLM consistency
                        if max_val > min_val:
                            normalized = (val - min_val) / (max_val - min_val) * 100
                            motives[stat_name] = round(normalized, 1)
        except Exception:
            pass # Fail gracefully if motives can't be read

        sim_name = f"{getattr(sim_info, 'first_name', '')} {getattr(sim_info, 'last_name', '')}".strip()
        state["sims"].append({
            "id": sim.id,
            "name": sim_name,
            "mood": mood,
            "is_sleeping": is_sleeping,
            "current_actions": current_actions,
            "wants": wants,
            "motives": motives,
            "is_llm_action_executing": is_llm_action_executing,
            "is_llm_action_queued": is_llm_action_queued,
            "nearby_objects": nearby_objects
        })
        
    return state

def network_worker():
    """Runs in the background, forwarding state to the sidecar and fetching commands."""
    global LAST_NETWORK_ERROR
    while True:
        state = outgoing_queue.get()
        if state is None:
            break # Exit signal received
            
        try:
            req = urllib.request.Request(SIDECAR_URL, method="POST")
            req.add_header('Content-Type', 'application/json; charset=utf-8')
            jsondata = json.dumps(state).encode('utf-8')
            
            # Send state and wait for response (timeout is increased for local LLMs)
            response = urllib.request.urlopen(req, jsondata, timeout=60)
            resp_data = json.loads(response.read().decode('utf-8'))
            
            incoming_queue.put(resp_data)
            LAST_NETWORK_ERROR = "OK"
        except Exception as e:
            # Capture the network error so we can view it in the game!
            LAST_NETWORK_ERROR = f"{type(e).__name__}: {e}"

def execute_command(command):
    """Translates the LLM's generic string into a game interaction and pushes it."""
    global LAST_BRAIN_ERROR, LAST_ACTION_STATUS
    try:
        sim_id = int(command.get("sim_id") or 0)
        # 1. Handle Cancellation Command first
        if command.get("action") == "cancel":
            LAST_ACTION_STATUS = f"CANCELLED: Dropped action for Sim {sim_id}"
            llm_action = ACTIVE_LLM_ACTIONS.pop(sim_id, None)
            if llm_action is not None:
                try:
                    llm_action.cancel(1, "LLM Want Disappeared")
                except Exception:
                    pass 
            return
            
        # Check if Sim is already busy with an LLM action
        if sim_id in ACTIVE_LLM_ACTIONS:
            # Verify if it's still running
            active_sim_info = services.sim_info_manager().get(sim_id)
            active_sim = active_sim_info.get_sim_instance() if active_sim_info else None
            if active_sim:
                existing = ACTIVE_LLM_ACTIONS[sim_id]
                in_si = active_sim.si_state is not None and existing in active_sim.si_state
                in_queue = getattr(active_sim, 'queue', None) is not None and existing in active_sim.queue
                if in_si or in_queue:
                    LAST_ACTION_STATUS = f"SKIPPED: Sim {sim_id} is already busy with an LLM action"
                    return
                else:
                    ACTIVE_LLM_ACTIONS.pop(sim_id, None)

        # 2. Extract and scrub target_id
        raw_target = str(command.get("target_object_id", "0")).split(':')[0].strip()
        target_id = int(raw_target) if raw_target.isdigit() else 0
        interaction_name = str(command.get("interaction_name", ""))
        
        if not sim_id or not target_id:
            LAST_ACTION_STATUS = f"FAILED: Invalid IDs (Sim:{sim_id}, Target:{target_id})"
            return
            
        sim_info = services.sim_info_manager().get(sim_id)
        sim = sim_info.get_sim_instance() if sim_info else None
        if not sim: 
            LAST_ACTION_STATUS = f"FAILED: Sim {sim_id} not found/instanced"
            return
            
        target = services.object_manager().get(target_id)
        if not target:
            t_info = services.sim_info_manager().get(target_id)
            target = t_info.get_sim_instance() if t_info else None
            
        if not target: 
            LAST_ACTION_STATUS = f"FAILED: Target {target_id} not found"
            return
            
        # 3. Retrieve Affordances (handle both list and generator/method)
        affordance_attr = getattr(target, 'super_affordances', None)
        if callable(affordance_attr):
            try:
                affordances = list(target.super_affordances())
            except Exception:
                affordances = getattr(target, '_super_affordances', [])
        else:
            affordances = getattr(target, '_super_affordances', [])
            
        if not affordances: 
            LAST_ACTION_STATUS = f"FAILED: Target {target_id} has no interactions"
            return
        
        # 4. Smart Matching Logic
        search_words = interaction_name.lower().replace(" ", "_").split("_")
        scored_affordances = []
        
        for aff in affordances:
            aff_name = getattr(aff, '__name__', '').lower()
            score = 0
            # Full match is best
            if "_".join(search_words) in aff_name:
                score += 10
            # Individual word matches
            for word in search_words:
                if len(word) > 2 and word in aff_name:
                    score += 2
            
            if score > 0:
                scored_affordances.append((score, aff))
        
        # Sort by score descending
        scored_affordances.sort(key=lambda x: x[0], reverse=True)
        
        if scored_affordances:
            affordance_to_push = scored_affordances[0][1]
        else:
            # Fallback to first available if requested name is totally unknown
            affordance_to_push = next(iter(affordances))
            
        # 5. Push Interaction
        context = interactions.context.InteractionContext(
            sim,
            interactions.context.InteractionContext.SOURCE_AUTONOMY,
            interactions.priority.Priority.High,
            insert_strategy=interactions.context.QueueInsertStrategy.LAST
        )
        
        result = sim.push_super_affordance(affordance_to_push, target, context)
        if result:
            ACTIVE_LLM_ACTIONS[sim_id] = result.interaction
            obj_name = getattr(target, 'definition', target.__class__).__name__
            LAST_ACTION_STATUS = f"SUCCESS: Queued '{affordance_to_push.__name__}' on '{obj_name}'"
        else:
            obj_name = getattr(target, 'definition', target.__class__).__name__
            LAST_ACTION_STATUS = f"FAILED: Engine rejected '{affordance_to_push.__name__}' on '{obj_name}'"
    except Exception as e:
        LAST_BRAIN_ERROR = f"Execute Error: {e}"

def brain_tick(_):
    """Fired by the game engine every X in-game minutes."""
    global LAST_BRAIN_ERROR
    try:
        # 1. Process any incoming commands from the LLM
        while not incoming_queue.empty():
            try:
                payload = incoming_queue.get_nowait()
                for cmd in payload.get("commands", []):
                    execute_command(cmd)
            except queue.Empty:
                break
                
        # 2. Extract current state and send to background thread
        state = extract_game_state()
        if state:
            # Clear the outgoing queue before putting the fresh state in.
            # This ensures the background thread only processes the MOST RECENT state.
            with outgoing_queue.mutex:
                outgoing_queue.queue.clear()
            outgoing_queue.put(state)
        LAST_BRAIN_ERROR = "OK"
    except Exception as e:
        import traceback
        LAST_BRAIN_ERROR = traceback.format_exc()

@sims4.commands.Command('llm.start', command_type=sims4.commands.CommandType.Live)
def start_llm_mod(_connection=None):
    output = sims4.commands.CheatOutput(_connection)
    global bg_thread, brain_alarm
    
    if bg_thread is None or not bg_thread.is_alive():
        bg_thread = threading.Thread(target=network_worker, daemon=True)
        bg_thread.start()
        
    if brain_alarm is None:
        time_span = create_time_span(minutes=POLLING_INTERVAL_MINUTES)
        brain_alarm = alarms.add_alarm(
            alarm_owner, time_span, brain_tick, repeating=True)
            
        # Trigger the first tick immediately so we don't have to wait!
        brain_tick(None)
            
    output(f"LLM Polling started. (Interval: {POLLING_INTERVAL_MINUTES} in-game minutes)")

@sims4.commands.Command('llm.status', command_type=sims4.commands.CommandType.Live)
def status_llm_mod(_connection=None):
    output = sims4.commands.CheatOutput(_connection)
    output("--- LLM Mod Status ---")
    
    output("Brain Status:")
    for line in str(LAST_BRAIN_ERROR).split('\n'):
        output(line)
        
    output(f"Network Status: {LAST_NETWORK_ERROR}")
    output(f"Last Action Status: {LAST_ACTION_STATUS}")
    output(f"Outgoing Queue Size: {outgoing_queue.qsize()}")