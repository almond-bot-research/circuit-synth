"""Standalone pcbnew scripts.

These scripts run under a Python that can import pcbnew (KiCad's bundled
interpreter on macOS, the system interpreter on Linux), which is generally
not the environment circuit-synth is installed in. They are therefore
self-contained: stdlib + pcbnew only, importing their shared helpers via a
sys.path insertion of this directory.

Run them through the `cs pcb ...` CLI, which locates a pcbnew-capable
interpreter and re-executes the script file with the right environment
(KICAD_CLI, CIRCUIT_SYNTH_LIB), or directly:

    cs kicad-py $(python -c "import circuit_synth.pcb.scripts as s; print(s.__path__[0])")/sync_pcb.py ...
"""
