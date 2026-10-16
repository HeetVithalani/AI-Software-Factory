"""
Unit tests for factory.intake.source — canonical GitHub URL + revision
parsing (fail-closed) and deterministic tool-id derivation.

All tests are offline; no network access occurs.
"""

from __future__ import annotations

import pytest

from factory.errors import IntakeError
from factory.intake.source import (
    GitHubSource,
    InvalidSourceError,
    derive_tool_id,
    parse_github_source,
    validate_revision_sha,
)

SHA1 = "1" * 40
SHA2 = "a" * 40


class TestValidSources:
    def test_canonical_url_and_pinned_sha(self) -> None:
        src = parse_github_source("https://github.com/openai/skills", SHA1)
        assert isinstance(src, GitHubSource)
        assert src.owner == "openai"
        assert src.repo == "skills"
        assert src.canonical_url == "https://github.com/openai/skills"
        assert src.revision_sha == SHA1

    def test_archive_url_is_derived_restricted_endpoint(self) -> None:
        src = parse_github_source("https://github.com/openai/skills", SHA1)
        assert src.archive_url == (
            f"https://codeload.github.com/openai/skills/tar.gz/{SHA1}"
        )

    def test_trailing_slash_and_git_suffix_normalized(self) -> None:
        src = parse_github_source("https://github.com/openai/skills.git/", SHA1)
        assert src.canonical_url == "https://github.com/openai/skills"
        assert src.repo == "skills"

    def test_uppercase_sha_normalized_to_lowercase(self) -> None:
        mixed = "ABCDEF" + "0" * 34
        src = parse_github_source("https://github.com/o/r", mixed)
        assert src.revision_sha == mixed.lower()

    def test_hyphenated_owner_and_dotted_repo_valid(self) -> None:
        src = parse_github_source("https://github.com/jane-doe/my.skill_v2", SHA2)
        assert src.owner == "jane-doe"
        assert src.repo == "my.skill_v2"


class TestRejectedUrls:
    @pytest.mark.parametrize("url", [
        "http://github.com/o/r",                          # non-HTTPS
        "ftp://github.com/o/r",                           # wrong scheme
        "https://gitlab.com/o/r",                         # wrong host
        "https://github.com.evil.com/o/r",                # host suffix attack
        "https://user:pass@github.com/o/r",               # credentials
        "https://github.com/o/r?ref=x",                   # query string
        "https://github.com/o/r#frag",                    # fragment
        "https://github.com:8080/o/r",                    # port
        "https://github.com/o/r/tree/main",               # sub-path
        "https://github.com/o/r/extra/deep",              # nested sub-path
        "https://github.com/o",                           # missing repo
        "https://github.com/",                            # missing both
        "",                                               # empty
        "   ",                                            # whitespace only
        "https://github.com /o/r",                        # embedded space
        "https://github.com/o/r ",                        # trailing space char
        "https://github.com/оwner/r",                     # non-ASCII owner
        "https:\\\\github.com\\o\\r",                     # backslashes
    ])
    def test_rejects_non_canonical(self, url: str) -> None:
        with pytest.raises(InvalidSourceError):
            parse_github_source(url, SHA1)

    @pytest.mark.parametrize("bad_sha", [
        "1" * 39,               # too short
        "1" * 41,               # too long
        "z" * 40,               # non-hex
        "main",                 # branch name
        "v1.0.0",               # tag
        "abc1234",              # short SHA
        "",                     # empty
    ])
    def test_rejects_non_pinned_revisions(self, bad_sha: str) -> None:
        with pytest.raises(InvalidSourceError):
            parse_github_source("https://github.com/o/r", bad_sha)

    def test_invalid_source_error_is_intake_error(self) -> None:
        with pytest.raises(IntakeError):
            parse_github_source("http://github.com/o/r", SHA1)

    def test_dot_only_repo_rejected(self) -> None:
        with pytest.raises(InvalidSourceError):
            parse_github_source("https://github.com/o/.", SHA1)


class TestRevisionSha:
    def test_valid_sha_passes(self) -> None:
        assert validate_revision_sha(SHA1) == SHA1

    def test_whitespace_padded_sha_stripped(self) -> None:
        assert validate_revision_sha(f"  {SHA2}\n") == SHA2


class TestToolId:
    def test_tool_id_is_deterministic_and_url_bound(self) -> None:
        a = derive_tool_id("agent_skill", "https://github.com/o/r")
        b = derive_tool_id("agent_skill", "https://github.com/o/r")
        assert a == b
        assert len(a) == 16

    def test_tool_id_changes_with_kind_and_url(self) -> None:
        base = derive_tool_id("agent_skill", "https://github.com/o/r")
        assert base != derive_tool_id("cli_tool", "https://github.com/o/r")
        assert base != derive_tool_id("agent_skill", "https://github.com/o/other")

    def test_revision_not_part_of_tool_id(self) -> None:
        # Same logical tool across revisions → same id.
        src1 = parse_github_source("https://github.com/o/r", SHA1)
        src2 = parse_github_source("https://github.com/o/r", SHA2)
        assert derive_tool_id("agent_skill", src1.canonical_url) == derive_tool_id(
            "agent_skill", src2.canonical_url
        )

    def test_case_variant_github_urls_share_one_identity(self) -> None:
        # GitHub owner/repo/host casing is insignificant: equivalent URLs
        # must not create separate logical tool identities.
        lower = derive_tool_id("agent_skill", "https://github.com/openai/skills")
        upper = derive_tool_id("agent_skill", "https://github.com/OpenAI/Skills")
        mixed = derive_tool_id("agent_skill", "https://GitHub.com/oPeNaI/sKiLlS")
        assert lower == upper == mixed

    def test_case_normalization_through_parse_and_derive(self) -> None:
        # End-to-end: parsed mixed-case source derives the same identity as
        # its canonical lowercase form.
        mixed = parse_github_source("https://github.com/OpenAI/Skills", SHA1)
        assert derive_tool_id("agent_skill", mixed.canonical_url) == (
            derive_tool_id("agent_skill", "https://github.com/openai/skills")
        )

    def test_distinct_repos_and_kinds_still_differ(self) -> None:
        # Normalization must not collapse genuinely distinct identities.
        a = derive_tool_id("agent_skill", "https://github.com/org/repo-a")
        b = derive_tool_id("agent_skill", "https://github.com/org/repo-b")
        assert a != b
        assert derive_tool_id("cli_tool", "https://github.com/org/repo-a") != a
