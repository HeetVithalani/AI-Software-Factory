"""
Tests for ToolCallProposal parsing and validation.
"""

from factory.llm.schemas.tool_call import ToolCallProposal

def test_tool_call_proposal_to_action_context():
    proposal = ToolCallProposal(
        action_type="shell.run",
        tool="shell",
        arguments={"command": "echo hello"},
        reason="test",
        expected_effect="test"
    )
    
    ctx = proposal.to_action_context()
    assert ctx.category == "shell.run"
    assert ctx.command == "echo hello"
