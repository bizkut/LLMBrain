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
import ui.ui_dialog_service
from ui.ui_dialog import PhoneRingType
import zone
from functools import wraps

POLLING_INTERVAL_MINUTES = 10
SIDECAR_URL = "http://127.0.0.1:8000/api/state"

# Thread-safe queues for communication between the game thread and network thread
outgoing_queue = queue.Queue()
incoming_queue = queue.Queue()

bg_thread = None
brain_alarm = None

ACTIVE_LLM_ACTIONS = {}
ACTIVE_DIALOGS = {}

# The game's alarm system requires an object that supports weak references as the 'owner'.
class _AlarmOwner:
    pass
alarm_owner = _AlarmOwner()

LAST_BRAIN_ERROR = "No runs yet"
LAST_NETWORK_ERROR = "No requests yet"
LAST_ACTION_STATUS = "No actions pushed yet"

def get_localized_string_context(localized_string):
    """Attempt to pull meaningful text/tokens out of a LocalizedString proto."""
    if localized_string is None:
        return {"hash": 0, "tokens": []}
    
    tokens = []
    try:
        # localized_string might be a proto or a wrapper.
        # We search for tokens which often contain Sim names or object names.
        raw_tokens = getattr(localized_string, 'tokens', [])
        for token in raw_tokens:
            if hasattr(token, 'raw_text'):
                tokens.append(str(token.raw_text))
            elif hasattr(token, 'number'):
                tokens.append(str(token.number))
    except Exception:
        pass
    
    return {
        "hash": getattr(localized_string, 'hash', 0),
        "tokens": tokens
    }

def extract_game_state():
    """Extracts basic information from controllable Sims."""
    client = services.client_manager().get_first_client()
    if not client:
        return None
        
    active_sims = client.selectable_sims
    state = {"sims": [], "active_dialogs": []}
    
    # 1. Collect Active Dialogs (Phone calls, popups)
    dialog_service = services.ui_dialog_service()
    if dialog_service is not None:
        for dialog_id, dialog in list(ACTIVE_DIALOGS.items()):
            try:
                # Check if the dialog is still active
                if dialog_id not in dialog_service._active_dialogs:
                    ACTIVE_DIALOGS.pop(dialog_id, None)
                    continue
                
                # Identify if this dialog is blocking/pausing the game
                is_modal = dialog.get_phone_ring_type() == 0 # Non-phone dialogs are usually modal
                
                # Identify which Sim this belongs to
                owner_name = "Unknown"
                if hasattr(dialog, 'owner') and dialog.owner is not None:
                    owner_name = f"{getattr(dialog.owner.sim_info, 'first_name', '')} {getattr(dialog.owner.sim_info, 'last_name', '')}".strip()
                
                # Extract basic info
                dialog_data = {
                    "id": dialog_id,
                    "owner": owner_name,
                    "tuning_name": dialog.__class__.__name__,
                    "is_urgent": is_modal,
                    "phone_call": dialog.get_phone_ring_type() != 0,
                    "title_hash": str(getattr(dialog.title, '_string_id', '0')),
                    "responses": []
                }
                
                # Extract button options
                responses = []
                if hasattr(dialog, '_get_responses_gen'):
                    responses = list(dialog._get_responses_gen())
                elif hasattr(dialog, 'responses'):
                    responses = dialog.responses
                
                for response in responses:
                    # In TS4, response IDs are things like 10001 (OK), 10002 (Cancel)
                    r_id = getattr(response, 'dialog_response_id', 0)
                    r_text = "OK"
                    if hasattr(response, 'text') and response.text is not None:
                        # We try to guess the text from common button IDs if we can't see the string
                        if r_id == 10001: r_text = "OK"
                        elif r_id == 10002: r_text = "Cancel"
                        else: r_text = f"Choice {r_id}"
                        
                    dialog_data["responses"].append({
                        "id": r_id,
                        "text": r_text
                    })
                
                state["active_dialogs"].append(dialog_data)
            except Exception:
                continue

    # 2. Collect Sims
    for sim_info in active_sims:
        sim = sim_info.get_sim_instance()
        if not sim:
            continue
            
        # Extract Mood, Moodlets, and Wants
        mood = "Unknown"
        moodlets = []
        wants = []
        try:
            # Grab the current mood
            current_mood = sim.get_mood()
            if current_mood is not None:
                mood = current_mood.__name__
                
            # Extract visible moodlets (Buffs) to give the LLM context on WHY they feel this way
            buff_component = getattr(sim_info, 'Buffs', None)
            if buff_component:
                for buff in buff_component:
                    if getattr(buff, 'visible', False):
                        b_name = buff.__class__.__name__
                        if b_name.lower().startswith('buff_'):
                            b_name = b_name[5:]
                        
                        # Format name: "DeathOfRelative" -> "Death Of Relative"
                        import re
                        temp_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', b_name).strip().title()
                        moodlets.append(temp_name)
        except Exception:
            pass
            
        try:
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
            is_llm_action_executing = False
            is_llm_action_queued = False
            current_actions = []
            
            if sim.si_state is not None:
                for si in sim.si_state:
                    si_name = si.__class__.__name__
                    # Skip common idle/posture interactions to focus on "real" actions
                    if any(x in si_name.lower() for x in ['idle', 'posture', 'stand', 'wait']):
                        continue
                    current_actions.append(si_name)
                    
            llm_action = ACTIVE_LLM_ACTIONS.get(sim.id)
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
                # Create a context to check what interactions are ACTUALLY available
                scan_context = interactions.context.InteractionContext(
                    sim,
                    interactions.context.InteractionContext.SOURCE_AUTONOMY,
                    interactions.priority.Priority.High
                )
                
                for obj in services.object_manager().get_all():
                    if obj.id == sim.id:
                        continue
                        
                    try:
                        # Skip objects that are invisible to the client (markers, controllers, etc.)
                        if not getattr(obj, 'visible_to_client', True):
                            continue
                            
                        # Skip objects hidden in inventories
                        if getattr(obj, 'parent', None) is not None:
                            continue
                            
                        obj_pos = getattr(obj, 'position', None)
                        if obj_pos is None:
                            continue
                            
                        # Calculate distance squared
                        dist_sq = (sim_pos.x - obj_pos.x)**2 + (sim_pos.y - obj_pos.y)**2 + (sim_pos.z - obj_pos.z)**2
                        
                        # Retrieve Affordances (passing context filters for currently valid actions)
                        affordance_attr = getattr(obj, 'super_affordances', None)
                        if callable(affordance_attr):
                            try:
                                affordances = list(obj.super_affordances(context=scan_context))
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
                                
                            # Only include interactions that are visible (have a display name)
                            if not hasattr(aff, 'display_name') or aff.display_name is None:
                                continue
                                
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
            
            # Sort by distance
            nearby_objects.sort(key=lambda x: x.get("dist", 999))
            nearby_objects = nearby_objects[:25]
        except Exception as e:
            nearby_objects.append({"error": str(e)})
            
        # Extract Motives (Needs)
        motives = {}
        try:
            tracker = getattr(sim, 'commodity_tracker', None)
            if tracker is not None:
                for stat in tracker:
                    if getattr(stat, 'visible', False) and getattr(stat, 'ui_sort_order', 0) > 0:
                        stat_name = stat.__class__.__name__
                        if stat_name.startswith('motive_'):
                            stat_name = stat_name[7:]
                        
                        val = stat.get_value()
                        min_val = getattr(stat, 'min_value', -100)
                        max_val = getattr(stat, 'max_value', 100)
                        
                        if max_val > min_val:
                            normalized = (val - min_val) / (max_val - min_val) * 100
                            motives[stat_name] = round(normalized, 1)
        except Exception:
            pass 

        # Extract Satisfaction Points and available Rewards
        satisfaction_points = 0
        try:
            satisfaction_points = sim_info.get_satisfaction_points()
            
            # If they have points, show them a virtual "Rewards Store" object
            if satisfaction_points > 0:
                tracker = getattr(sim_info, '_satisfaction_tracker', None)
                if tracker:
                    store_items = {}
                    # Limit to top 15 affordable rewards
                    count = 0
                    for reward, data in tracker.SATISFACTION_STORE_ITEMS.items():
                        if count >= 15: break
                        if data.cost <= satisfaction_points:
                            if reward.is_valid(sim_info):
                                r_name = reward.__name__
                                if r_name.startswith('reward_'): r_name = r_name[7:]
                                import re
                                clean_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', r_name).strip().title()
                                display_name = f"Buy {clean_name} ({data.cost}pts)"
                                store_items[display_name] = f"PURCHASE_{reward.guid64}"
                                count += 1
                    
                    if store_items:
                        nearby_objects.append({
                            "id": 999,
                            "name": "Rewards Store",
                            "dist": 0.0,
                            "interactions": store_items
                        })
        except Exception:
            pass

        sim_name = f"{getattr(sim_info, 'first_name', '')} {getattr(sim_info, 'last_name', '')}".strip()
        state["sims"].append({
            "id": sim.id,
            "name": sim_name,
            "mood": mood,
            "moodlets": moodlets,
            "satisfaction_points": satisfaction_points,
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
            
            # Send state and wait for response
            response = urllib.request.urlopen(req, jsondata, timeout=60)
            resp_data = json.loads(response.read().decode('utf-8'))
            
            incoming_queue.put(resp_data)
            LAST_NETWORK_ERROR = "OK"
        except Exception as e:
            LAST_NETWORK_ERROR = f"{type(e).__name__}: {e}"

def execute_command(command):
    """Translates the LLM's generic string into a game interaction and pushes it."""
    global LAST_BRAIN_ERROR, LAST_ACTION_STATUS
    try:
        # Handle Dialog Response (Phone calls)
        if "dialog_id" in command:
            d_id = int(command["dialog_id"])
            r_id = int(command["response_id"])
            if d_id in ACTIVE_DIALOGS:
                services.ui_dialog_service().dialog_respond(d_id, r_id)
                ACTIVE_DIALOGS.pop(d_id, None)
                LAST_ACTION_STATUS = f"SUCCESS: Responded to dialog {d_id} with {r_id}"
            else:
                LAST_ACTION_STATUS = f"FAILED: Dialog {d_id} no longer active"
            return

        sim_id = int(command.get("sim_id") or 0)
        # Handle Cancellation
        if command.get("action") == "cancel":
            LAST_ACTION_STATUS = f"CANCELLED: Dropped action for Sim {sim_id}"
            llm_action = ACTIVE_LLM_ACTIONS.pop(sim_id, None)
            if llm_action is not None:
                try:
                    llm_action.cancel(1, "LLM Want Disappeared")
                except Exception:
                    pass 
            return
            
        # Check Busy state
        if sim_id in ACTIVE_LLM_ACTIONS:
            active_sim_info = services.sim_info_manager().get(sim_id)
            active_sim = active_sim_info.get_sim_instance() if active_sim_info else None
            if active_sim:
                existing = ACTIVE_LLM_ACTIONS[sim_id]
                in_si = active_sim.si_state is not None and existing in active_sim.si_state
                in_queue = getattr(active_sim, 'queue', None) is not None and existing in active_sim.queue
                if in_si or in_queue:
                    LAST_ACTION_STATUS = f"SKIPPED: Sim {sim_id} is busy"
                    return
                else:
                    ACTIVE_LLM_ACTIONS.pop(sim_id, None)

        sim_info = services.sim_info_manager().get(sim_id)
        
        # Handle virtual "Rewards Store" purchase
        interaction_name = str(command.get("interaction_name", ""))
        if interaction_name.startswith("PURCHASE_"):
            try:
                reward_guid = int(interaction_name.split("_")[1])
                tracker = getattr(sim_info, '_satisfaction_tracker', None)
                if tracker:
                    tracker.purchase_satisfaction_reward(reward_guid)
                    LAST_ACTION_STATUS = f"SUCCESS: Sim {sim_id} purchased reward {reward_guid}"
                else:
                    LAST_ACTION_STATUS = f"FAILED: No satisfaction tracker for Sim {sim_id}"
            except Exception as e:
                LAST_ACTION_STATUS = f"FAILED: Purchase error: {e}"
            return

        # Handle Object Interaction
        raw_target = str(command.get("target_object_id", "0")).split(':')[0].strip()
        target_id = int(raw_target) if raw_target.isdigit() else 0
        
        if not sim_id or not target_id:
            LAST_ACTION_STATUS = f"FAILED: Invalid IDs (Sim:{sim_id}, Target:{target_id})"
            return
            
        if not sim_info: 
            LAST_ACTION_STATUS = f"FAILED: Sim {sim_id} not found"
            return
        sim = sim_info.get_sim_instance()
        if not sim: 
            LAST_ACTION_STATUS = f"FAILED: Sim {sim_id} has no instance"
            return
            
        target = services.object_manager().get(target_id)
        if not target:
            t_info = services.sim_info_manager().get(target_id)
            target = t_info.get_sim_instance() if t_info else None
            
        if not target: 
            LAST_ACTION_STATUS = f"FAILED: Target {target_id} not found"
            return
            
        affordance_attr = getattr(target, 'super_affordances', None)
        if callable(affordance_attr):
            try:
                scan_context = interactions.context.InteractionContext(
                    sim,
                    interactions.context.InteractionContext.SOURCE_AUTONOMY,
                    interactions.priority.Priority.High
                )
                affordances = list(target.super_affordances(context=scan_context))
            except Exception:
                affordances = getattr(target, '_super_affordances', [])
        else:
            affordances = getattr(target, '_super_affordances', [])
            
        if not affordances: 
            LAST_ACTION_STATUS = f"FAILED: Target {target_id} has no interactions"
            return
        
        search_words = interaction_name.lower().replace(" ", "_").split("_")
        scored_affordances = []
        for aff in affordances:
            aff_name = getattr(aff, '__name__', '').lower()
            score = 0
            if "_".join(search_words) in aff_name: score += 10
            for word in search_words:
                if len(word) > 2 and word in aff_name: score += 2
            if score > 0: scored_affordances.append((score, aff))
        
        scored_affordances.sort(key=lambda x: x[0], reverse=True)
        affordance_to_push = scored_affordances[0][1] if scored_affordances else next(iter(affordances))
            
        context = interactions.context.InteractionContext(
            sim,
            interactions.context.InteractionContext.SOURCE_AUTONOMY,
            interactions.priority.Priority.High,
            insert_strategy=interactions.context.QueueInsertStrategy.LAST
        )
        
        result = sim.push_super_affordance(affordance_to_push, target, context)
        if result:
            ACTIVE_LLM_ACTIONS[sim_id] = result.interaction
            LAST_ACTION_STATUS = f"SUCCESS: Queued '{affordance_to_push.__name__}'"
        else:
            LAST_ACTION_STATUS = f"FAILED: Engine rejected interaction"
    except Exception as e:
        LAST_BRAIN_ERROR = f"Execute Error: {e}"

def brain_tick(_):
    """Fired by the game engine every X in-game minutes."""
    global LAST_BRAIN_ERROR
    try:
        while not incoming_queue.empty():
            try:
                payload = incoming_queue.get_nowait()
                for cmd in payload.get("commands", []):
                    execute_command(cmd)
            except queue.Empty:
                break
                
        state = extract_game_state()
        if state:
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
        brain_alarm = alarms.add_alarm(alarm_owner, time_span, brain_tick, repeating=True)
        brain_tick(None)
    output(f"LLM Polling started.")

@sims4.commands.Command('llm.status', command_type=sims4.commands.CommandType.Live)
def status_llm_mod(_connection=None):
    output = sims4.commands.CheatOutput(_connection)
    output(f"--- LLM Mod Status ---\nBrain: {LAST_BRAIN_ERROR}\nNetwork: {LAST_NETWORK_ERROR}\nLast Action: {LAST_ACTION_STATUS}")

def inject_to(target_object, target_function_name):
    def _inject_to(new_function):
        target_function = getattr(target_object, target_function_name)
        @wraps(target_function)
        def _inject(*args, **kwargs):
            return new_function(target_function, *args, **kwargs)
        setattr(target_object, target_function_name, _inject)
        return new_function
    return _inject_to

@inject_to(ui.ui_dialog_service.UiDialogService, 'dialog_show')
def llm_on_dialog_show(original_function, self, dialog, phone_ring_type, *args, **kwargs):
    ACTIVE_DIALOGS[dialog.dialog_id] = dialog
    return original_function(self, dialog, phone_ring_type, *args, **kwargs)

@inject_to(zone.Zone, 'on_loading_screen_animation_finished')
def llm_on_zone_load(original_function, self, *args, **kwargs):
    result = original_function(self, *args, **kwargs)
    global brain_alarm
    if brain_alarm is not None:
        brain_alarm = None
        time_span = create_time_span(minutes=POLLING_INTERVAL_MINUTES)
        brain_alarm = alarms.add_alarm(alarm_owner, time_span, brain_tick, repeating=True)
        brain_tick(None)
    return result
