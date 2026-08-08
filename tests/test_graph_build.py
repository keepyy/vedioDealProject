import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DEPS = ROOT / "_pydeps"
if DEPS.exists():
    sys.path.insert(0, str(DEPS))
sys.path.insert(0, str(ROOT))

from app.agents.workflow import build_graph, WorkflowState

g = build_graph()
assert g is not None
print('GRAPH BUILT OK')
print('Nodes:', list(g.nodes.keys()) if hasattr(g, 'nodes') else '(compiled)')

# 画一下图结构（mermaid 风格文字描述）
print('INTERRUPT_BEFORE:', g.interrupt_before if hasattr(g, 'interrupt_before') else '(unknown)')
