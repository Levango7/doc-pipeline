# debug_data_flow.py
"""Debug the dependency resolution in the researcher→fetcher pipeline"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from pipeline_core import PipelineOrchestrator
from pipeline_core.scheduler import Scheduler

o = PipelineOrchestrator(checkpoint_dir="checkpoints")
o.register_agents()

s = Scheduler("pipelines")
plan = s.parse("docgen")

print("=== Node Dependencies ===")
for i, level in enumerate(plan.levels):
    for node in level:
        print(f"  Level {i}: {node.agent_name} -> deps={node.dependencies}")

task = o.run_plan(plan, "input_db.md", "debug_data_flow", wait=True)
print(f"\nStatus: {task.status}")
print(f"Duration: {getattr(task, 'duration_ms', 0):.1f}ms")

# Check dag_nodes for key structure
print(f"\nDAG node keys: {list(task.dag_nodes.keys())}")

# Check all result keys with pool awareness
print(f"Result keys: {list(task.result.keys())}")

for k in sorted(task.result.keys()):
    if "researcher" in k:
        r = task.result[k]
        if isinstance(r, dict):
            results = r.get("results", [])
            print(f"  {k}: type=dict, keys={list(r.keys())}, result_count={len(results)}")
            if results:
                print(f"    Sample URL: {results[0].get('url', 'N/A')}")

print("\n=== Done ===")
