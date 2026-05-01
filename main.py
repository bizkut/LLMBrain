import sims4.commands
import services
import traceback

# Catch any errors in llm_brain so we can print them in the game!
try:
    import llm_brain
    LLM_ERROR = None
except Exception as e:
    LLM_ERROR = traceback.format_exc()

@sims4.commands.Command('helloworld', command_type=sims4.commands.CommandType.Live)
def helloworld(_connection=None):
    output = sims4.commands.CheatOutput(_connection)
    output("This is my first script mod")
    
    if LLM_ERROR:
        output("ERROR LOADING LLM BRAIN:")
        for line in LLM_ERROR.split('\n'):
            output(line)
        return
        
    try:
        client = services.client_manager().get_first_client()
        if client is not None and client.active_sim is not None:
            output(f"Active Sim: {client.active_sim.sim_info.full_name}")
        else:
            output("No active Sim found. Are you in Live Mode?")
    except Exception as e:
        output(f"Execution Error: {e}")
