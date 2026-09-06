"""
Tests for end-to-end LLM vertical slice.
"""

import pytest
import subprocess
import os
from pathlib import Path

@pytest.mark.integration
def test_builder_hello_api_vertical_slice():
    """
    Runs the factory builder end-to-end using the CLI, asserting that it successfully creates 
    a file and exits cleanly. This requires a running Ollama instance with qwen2.5-coder:7b.
    """
    cwd = Path("D:/AI-Software-Factory")
    test_project = cwd / "projects" / "test-hello-api"
    
    # Ensure test project dir exists
    test_project.mkdir(parents=True, exist_ok=True)
    
    # Cleanup previous run if exists
    hello_file = test_project / "hello.py"
    if hello_file.exists():
        hello_file.unlink()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    
    # Run the builder slice via CLI
    process = subprocess.run(
        [
            "python", "-m", "factory", "run", "projects/test-hello-api",
            "--task-desc", "Write a python script in hello.py that prints 'Hello World' and save it. Then set completed=true."
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180
    )
    
    assert process.returncode == 0, f"Factory run failed:\nSTDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}"
    
    assert hello_file.exists(), "The LLM failed to write the requested file."
    content = hello_file.read_text()
    assert "Hello World" in content, "The written file did not contain the expected content."

