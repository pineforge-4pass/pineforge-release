"""Rename regressions using real frozen facts/policy and the actual validators."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import facts_descriptions as descriptions  # noqa: E402
import facts_validate as facts_validator  # noqa: E402
import release_artifacts as artifacts  # noqa: E402
import release_contract as contract  # noqa: E402
import repo_descriptions as policy_validator  # noqa: E402

ORGS = ("pineforge-4pass", "tunaxis")
BAD_ORGS = ("other", "pineforge-4pass-x", "xpineforge-4pass", "tunaxis-x", "Tunaxis", "TUNAXIS", "")
BAD_IDENTITY_ORGS = ("other", "pineforge-4pass-x", "xpineforge-4pass", "tunaxis-x")
FACTS = ROOT / "facts/facts.json"
POLICY = ROOT / "facts/repo-descriptions.json"


def url_org(value, org):
    parts = value.split("/")
    parts[3] = org
    return "/".join(parts)


def policy_org(value, org):
    document = copy.deepcopy(value)
    sources = list(document["licenses"].values())
    for row in document["repositories"]:
        row["repo"] = org + "/" + row["repo"].split("/", 1)[1]
        sources.append(row["source"])
    for source in sources:
        source["repo"] = org + "/" + source["repo"].split("/", 1)[1]
        source["url"] = url_org(source["url"], org)
    return document


class FactsOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.canonical = json.loads(FACTS.read_bytes())

    def test_unchanged_real_facts_validate_and_retain_their_digest(self):
        validated, digest = facts_validator.load_facts(FACTS)
        self.assertEqual(digest, hashlib.sha256(FACTS.read_bytes()).hexdigest())
        self.assertEqual(validated, self.canonical)

    def test_release_sources_accept_exact_old_and_new_orgs(self):
        for org in ORGS:
            with self.subTest(org=org):
                value = copy.deepcopy(self.canonical)
                for release in value["releases"].values():
                    release["source"] = url_org(release["source"], org)
                validated = facts_validator.validate_facts(value)
                for version, release in validated["releases"].items():
                    self.assertEqual(release["source"],
                                     f"https://github.com/{org}/pineforge-engine/blob/v{version}/README.md#validation-scoreboard")

    def test_inventory_source_accepts_exact_old_and_new_orgs(self):
        for org in ORGS:
            with self.subTest(org=org):
                value = copy.deepcopy(self.canonical)
                expected = url_org(self.canonical["inventory"]["source"], org)
                value["inventory"]["source"] = expected
                validated = facts_validator.validate_facts(value)
                self.assertEqual(validated["inventory"]["source"], expected)

    def test_release_source_rejects_third_org_each_lookalike_case_and_empty_org(self):
        for org in BAD_ORGS:
            with self.subTest(org=org):
                value = copy.deepcopy(self.canonical)
                release = next(iter(value["releases"].values()))
                release["source"] = url_org(release["source"], org)
                with self.assertRaisesRegex(facts_validator.InputError,
                                            r"^facts schema validation failed \(pattern\)$"):
                    facts_validator.validate_facts(value)

    def test_inventory_source_rejects_third_org_each_lookalike_case_and_empty_org(self):
        for org in BAD_ORGS:
            with self.subTest(org=org):
                value = copy.deepcopy(self.canonical)
                value["inventory"]["source"] = url_org(value["inventory"]["source"], org)
                with self.assertRaisesRegex(facts_validator.InputError, "^inventory source identity differs$"):
                    facts_validator.validate_facts(value)

    def test_release_source_still_requires_the_entire_exact_repository_version_and_path(self):
        for org in ORGS:
            for mutation in ("trailing_slash", "query", "repository", "version", "fragment"):
                with self.subTest(org=org, mutation=mutation):
                    value = copy.deepcopy(self.canonical)
                    release = value["releases"]["1.0.1"]
                    source = url_org(release["source"], org)
                    if mutation == "trailing_slash":
                        source += "/"
                    elif mutation == "query":
                        source += "?ref=main"
                    elif mutation == "repository":
                        source = source.replace("/pineforge-engine/", "/other/")
                    elif mutation == "version":
                        source = source.replace("/v1.0.1/", "/v1.0.2/")
                    else:
                        source = source.replace("#validation-scoreboard", "#other")
                    release["source"] = source
                    with self.assertRaisesRegex(facts_validator.InputError, "^release source identity differs$"):
                        facts_validator.validate_facts(value)

    def test_inventory_source_still_requires_the_entire_exact_provenance_and_suffix(self):
        for org in ORGS:
            for mutation in ("trailing_slash", "repository", "commit", "release", "suffix"):
                with self.subTest(org=org, mutation=mutation):
                    value = copy.deepcopy(self.canonical)
                    inventory = value["inventory"]
                    source = url_org(inventory["source"], org)
                    if mutation == "trailing_slash":
                        source += "/"
                    elif mutation == "repository":
                        source = source.replace("/pineforge-engine/", "/other/")
                    elif mutation == "commit":
                        source = source.replace(inventory["sourceCommit"], "0" * 40)
                    elif mutation == "release":
                        source = source.replace("historical release " + inventory["sourceRelease"],
                                                "historical release 99.0.0")
                    else:
                        source = source.replace("not registry row or slug totals.", "registry totals.")
                    inventory["source"] = source
                    with self.assertRaisesRegex(facts_validator.InputError, "^inventory source identity differs$"):
                        facts_validator.validate_facts(value)


class PolicyOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.facts = json.loads(FACTS.read_bytes())
        self.policy = json.loads(POLICY.read_bytes())

    def render(self, policy):
        return policy_validator.render_document(self.facts, policy, "a" * 64, "b" * 64)

    def test_frozen_policy_and_both_org_forms_validate_under_either_api_mapping(self):
        for api_org in ORGS:
            mapping = {role: f"{api_org}/pineforge-{role}" for role in policy_validator.ROLES}
            with patch.object(policy_validator, "PUBLIC_REPOSITORIES", mapping):
                for data_org in ORGS:
                    with self.subTest(api_org=api_org, data_org=data_org):
                        document = self.render(policy_org(self.policy, data_org))
                        self.assertEqual({row["repo"] for row in document["repositories"]},
                                         {f"{data_org}/pineforge-{role}" for role in
                                          ("engine", "codegen-oss", "corpus", "hpo", "release", "backtest-mcp")})
                frozen = policy_validator.render(FACTS, POLICY)
                self.assertEqual(frozen["policy_sha256"], hashlib.sha256(POLICY.read_bytes()).hexdigest())
                self.assertEqual([row["repo"] for row in frozen["repositories"]],
                                 [row["repo"] for row in self.policy["repositories"]])

    def test_license_url_accepts_either_org_for_the_same_pinned_repository(self):
        for repo_org in ORGS:
            for source_org in ORGS:
                with self.subTest(repo_org=repo_org, source_org=source_org):
                    value = policy_org(self.policy, repo_org)
                    for source in [*value["licenses"].values(),
                                   *(row["source"] for row in value["repositories"])]:
                        source["url"] = url_org(source["url"], source_org)
                    document = self.render(value)
                    self.assertEqual(len(document["repositories"]), 6)
                    self.assertEqual(document["repositories"][0]["public_source"]["url"],
                                     value["repositories"][0]["source"]["url"])

    def test_source_repository_rejects_other_orgs_and_lookalikes(self):
        for org in BAD_IDENTITY_ORGS:
            with self.subTest(org=org):
                value = copy.deepcopy(self.policy["repositories"][0]["source"])
                value["repo"] = org + "/" + value["repo"].split("/", 1)[1]
                value["url"] = url_org(value["url"], org)
                with self.assertRaisesRegex(facts_validator.InputError, "^source repository is not confirmed public$"):
                    policy_validator.source(value)

    def test_row_and_source_aliases_still_bind_the_same_repository(self):
        for row_org in ORGS:
            for source_org in ORGS:
                with self.subTest(row_org=row_org, source_org=source_org):
                    value = policy_org(self.policy, source_org)
                    for row in value["repositories"]:
                        row["repo"] = row_org + "/" + row["repo"].split("/", 1)[1]
                    document = self.render(value)
                    self.assertEqual([row["repo"] for row in document["repositories"]],
                                     [row["repo"] for row in value["repositories"]])
                    value["repositories"][0]["repo"] = f"{row_org}/pineforge-hpo"
                    with self.assertRaisesRegex(facts_validator.InputError, "^unverified or nonpublic repository$"):
                        self.render(value)

    def test_empty_source_org_retains_the_invalid_identity_diagnostic(self):
        value = copy.deepcopy(self.policy["repositories"][0]["source"])
        value["repo"] = "/" + value["repo"].split("/", 1)[1]
        with self.assertRaisesRegex(facts_validator.InputError, "^invalid public repository identity$"):
            policy_validator.source(value)

    def test_license_url_rejects_other_orgs_repository_path_and_commit(self):
        original = self.policy["repositories"][0]["source"]
        urls = [(org, url_org(original["url"], org)) for org in BAD_IDENTITY_ORGS]
        urls.extend((("repository", original["url"].replace("/pineforge-engine/", "/pineforge-hpo/")),
                     ("path", original["url"] + "/"),
                     ("commit", original["url"].replace(original["commit"], "0" * 40))))
        for name, url in urls:
            with self.subTest(name=name):
                value = copy.deepcopy(original)
                value["url"] = url
                with self.assertRaisesRegex(facts_validator.InputError, "^LICENSE source URL does not match its repository/commit$"):
                    policy_validator.source(value)

    def test_roles_cannot_be_swapped_under_either_org(self):
        for org in ORGS:
            with self.subTest(org=org):
                value = policy_org(self.policy, org)
                engine = next(row for row in value["repositories"] if row["role"] == "engine")
                hpo = next(row for row in value["repositories"] if row["role"] == "hpo")
                engine["role"], hpo["role"] = hpo["role"], engine["role"]
                with self.assertRaisesRegex(facts_validator.InputError, "^role does not match its confirmed-public repository$"):
                    self.render(value)


class DescriptionOrganizationTests(unittest.TestCase):
    def test_live_cli_readback_accepts_either_org_for_the_frozen_rows(self):
        for org in ORGS:
            with self.subTest(org=org):
                document = policy_validator.render(FACTS, POLICY)
                responses = {
                    "repos/" + row["repo"]: {"full_name": f"{org}/pineforge-{row['role']}",
                                            "private": False, "description": row["expected"]}
                    for row in document["repositories"]}

                def github(command, **kwargs):
                    return subprocess.CompletedProcess(command, 0,
                                                       json.dumps(responses[command[-1]]).encode(), b"")

                with patch.object(policy_validator.shutil, "which", return_value="/fixture/gh"), \
                        patch.object(policy_validator.os, "access", return_value=True), \
                        patch.object(policy_validator.subprocess, "run", side_effect=github) as boundary:
                    code = policy_validator.check_live(document, "gh")
                self.assertEqual(code, 0)
                self.assertEqual(boundary.call_count, 6)
                self.assertEqual([call.args[0][-1] for call in boundary.call_args_list], list(responses))
                self.assertTrue(all(row["status"] == "match" for row in document["repositories"]))

    def test_live_cli_readback_rejects_other_orgs_other_repositories_and_empty(self):
        for identity in [*(f"{org}/pineforge-engine" for org in BAD_IDENTITY_ORGS),
                         *(f"{org}/pineforge-hpo" for org in ORGS), ""]:
            with self.subTest(identity=identity):
                document = policy_validator.render(FACTS, POLICY)
                responses = {
                    "repos/" + row["repo"]: {"full_name": row["repo"], "private": False,
                                            "description": row["expected"]}
                    for row in document["repositories"]}
                responses["repos/" + document["repositories"][0]["repo"]]["full_name"] = identity

                def github(command, **kwargs):
                    return subprocess.CompletedProcess(command, 0,
                                                       json.dumps(responses[command[-1]]).encode(), b"")

                with patch.object(policy_validator.shutil, "which", return_value="/fixture/gh"), \
                        patch.object(policy_validator.os, "access", return_value=True), \
                        patch.object(policy_validator.subprocess, "run", side_effect=github):
                    code = policy_validator.check_live(document, "gh")
                self.assertEqual(code, 2)
                row = document["repositories"][0]
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["error"], "invalid public repository identity" if not identity
                                 else "GitHub did not confirm the exact public repository")

    def test_frozen_policy_rows_and_canonical_api_names_work_with_either_mapping(self):
        rows = json.loads(POLICY.read_bytes())["repositories"]
        for api_org in ORGS:
            mapping = {role: f"{api_org}/pineforge-{role}" for role in policy_validator.ROLES}
            with patch.object(descriptions, "PUBLIC_REPOSITORIES", mapping):
                for row in rows:
                    for response_org in ORGS:
                        with self.subTest(api_org=api_org, role=row["role"], response_org=response_org):
                            api = descriptions.GitHub()
                            response = {"full_name": f"{response_org}/pineforge-{row['role']}",
                                        "private": False, "description": "public description"}
                            with patch.object(api, "request", return_value=response) as boundary:
                                actual = api.description(row)
                            self.assertEqual(actual, "public description")
                            boundary.assert_called_once_with("/repos/" + row["repo"])

    def test_public_readback_rejects_third_org_lookalikes_other_repo_and_empty(self):
        for org in ORGS:
            for identity in [*(f"{bad}/pineforge-engine" for bad in BAD_IDENTITY_ORGS),
                             f"{org}/pineforge-hpo", ""]:
                with self.subTest(org=org, identity=identity):
                    api = descriptions.GitHub()
                    with patch.object(api, "request", return_value={"full_name": identity, "private": False}):
                        message = ("^invalid public repository identity$" if not identity
                                   else "^GitHub did not confirm the exact public repository$")
                        error = facts_validator.InputError if not identity else descriptions.ApiError
                        with self.assertRaisesRegex(error, message):
                            api.public_repository(f"{org}/pineforge-engine")

    def test_description_role_rejects_another_confirmed_public_repository(self):
        for org in ORGS:
            with self.subTest(org=org):
                api = descriptions.GitHub()
                with self.assertRaisesRegex(facts_validator.InputError, "^public role identity differs$"):
                    api.description({"role": "engine", "repo": f"{org}/pineforge-hpo"})

    def test_patch_target_accepts_both_orgs_only_for_managed_roles(self):
        for org in ORGS:
            for role in ("engine", "codegen-oss", "corpus", "hpo"):
                with self.subTest(org=org, role=role), patch.dict(os.environ, FACTS_WRITE_TOKEN="test-write"):
                    api = descriptions.GitHub()
                    api.opener = Mock()
                    api.opener.open.return_value = io.BytesIO(b'{"description":"updated public text"}')
                    response = api.request(f"/repos/{org}/pineforge-{role}", description="updated public text")
                    self.assertEqual(response["description"], "updated public text")
                    request = api.opener.open.call_args.args[0]
                    self.assertEqual(request.full_url, f"https://api.github.com/repos/{org}/pineforge-{role}")
                    self.assertEqual(request.method, "PATCH")
                    self.assertEqual(json.loads(request.data), {"description": "updated public text"})
        for identity in [*(f"{org}/pineforge-engine" for org in BAD_ORGS),
                         *(f"{org}/pineforge-release" for org in ORGS), ""]:
            with self.subTest(identity=identity):
                api = descriptions.GitHub()
                api.opener = Mock()
                with self.assertRaisesRegex(facts_validator.InputError, "^write target is not managed$"):
                    api.request("/repos/" + identity, description="updated public text")
                api.opener.open.assert_not_called()


class ArtifactOrganizationTests(unittest.TestCase):
    def asset(self, repo, source):
        raw = b"nonempty release archive fixture"
        release = {"tag_name": "v1.6.0", "assets": [{
            "name": "engine.tar.gz", "state": "uploaded", "browser_download_url": source,
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}]}
        client = Mock()
        client.download.return_value = raw
        result = artifacts.asset(client, release, "engine.tar.gz", repo)
        return result, client

    def test_asset_source_accepts_both_orgs_without_changing_the_download_target(self):
        for api_org in ORGS:
            for source_org in ORGS:
                with self.subTest(api_org=api_org, source_org=source_org):
                    result, client = self.asset(f"{api_org}/pineforge-engine",
                                                f"https://github.com/{source_org}/pineforge-engine/releases/download/v1.6.0/engine.tar.gz")
                    self.assertEqual(result, b"nonempty release archive fixture")
                    client.download.assert_called_once_with(
                        f"https://github.com/{api_org}/pineforge-engine/releases/download/v1.6.0/engine.tar.gz",
                        {"github.com"})

    def test_asset_source_rejects_other_orgs_each_lookalike_case_empty_and_path_changes(self):
        sources = [(org, f"https://github.com/{org}/pineforge-engine/releases/download/v1.6.0/engine.tar.gz")
                   for org in BAD_ORGS]
        for org in ORGS:
            sources.extend(((org + "_repository", f"https://github.com/{org}/pineforge-hpo/releases/download/v1.6.0/engine.tar.gz"),
                            (org + "_path", f"https://github.com/{org}/pineforge-engine/releases/download/v1.6.0/engine.tar.gz/")))
        sources.extend((("null", None), ("list", []), ("object", {})))
        for api_org in ORGS:
            for name, source in sources:
                with self.subTest(api_org=api_org, name=name):
                    with self.assertRaisesRegex(contract.Invalid, "^asset source identity differs$"):
                        self.asset(f"{api_org}/pineforge-engine", source)
