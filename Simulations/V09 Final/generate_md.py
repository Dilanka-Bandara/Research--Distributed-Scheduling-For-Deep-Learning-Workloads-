import os

files = [
    ".dockerignore", ".env", ".env.pc1.template", ".env.pc2.template", ".gitignore", "Dockerfile",
    "PC2_SETUP_AND_AGENT_GUIDE.md", "README.md", "analyze_results.py", "brain.py",
    "common.py", "compare_all.py", "config.py", "dispatcher.py", "docker-compose.pc1.yml",
    "docker-compose.pc2.yml", "docker-compose.yml", "fast_solver.py", "fft_scheduler_proc.py",
    "ilp_core.py", "node_agent.py", "orchestrator.py", "preflight.ps1", "preflight.py",
    "run_all_36.py", "sync_to_pc2.ps1", "test_connection.py", "trace_replayer.py", "workload.py"
]

out_path = "e:\\Research testing V08 using Antigravity\\Option B\\project_context.md"

with open(out_path, "w", encoding="utf-8") as f:
    f.write("# Project Context\n\n")
    f.write("## File Structure\n\n```text\n.\n")
    for idx, fname in enumerate(files):
        prefix = "└── " if idx == len(files) - 1 else "├── "
        f.write(f"{prefix}{fname}\n")
    f.write("```\n\n")
    
    f.write("## Files\n\n")
    for fname in files:
        f.write(f"### {fname}\n\n")
        ext = os.path.splitext(fname)[1].lstrip('.')
        lang = ""
        if ext == "py": lang = "python"
        elif ext in ["yml", "yaml"]: lang = "yaml"
        elif ext == "md": lang = "markdown"
        elif ext == "ps1": lang = "powershell"
        elif ext == "sh": lang = "bash"
        elif ext == "json": lang = "json"
        
        f.write(f"```{lang}\n")
        try:
            with open(os.path.join("e:\\Research testing V08 using Antigravity\\Option B", fname), "r", encoding="utf-8") as src:
                f.write(src.read())
        except Exception as e:
            f.write(f"Error reading file: {e}")
        f.write("\n```\n\n")
