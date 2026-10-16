"""
factory.skills
==============
Project-scoped skill runtime (Phase 2c).

* ``bindings`` — per-project enablement of installed skill revisions
  (explicit, human-driven, audited; never executes anything).
* ``provider`` — safe skill-context provider: verifies, ranks, bounds
  and attributes skill reference material for LLM requests.

Skills are Markdown reference context only. They are never executed,
never registered as tools, and never grant broker capabilities.
"""

from factory.skills.bindings import (  # noqa: F401
    disable_skill,
    enable_skill,
    list_bindings,
    list_installed_skills,
    project_key_for,
    validate_enableable,
)
from factory.skills.provider import (  # noqa: F401
    SkillContextProvider,
    SkillSelection,
    parse_skill_markdown,
)
